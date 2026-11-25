"""
RAG知识库智能体 RAGKnowledgeAgent
职责：基于向量数据库的知识检索与问答

核心功能：
1. 知识库构建：将商旅相关文档向量化并存储到Milvus Lite
2. 语义检索：根据用户查询检索最相关的知识片段
3. 知识问答：结合检索到的知识和LLM生成准确答案
4. 知识管理：支持添加、更新、删除知识库内容

技术栈：
- Milvus Lite: 轻量级向量数据库（本地存储）
- sentence-transformers: 文本向量化模型
- LLM: 用户配置的豆包模型用于生成答案

安装：
pip install milvus sentence-transformers
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict
import json
import logging
import os
from pathlib import Path

# Add project root to sys.path
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

_GRPC_MAX_MS = '2147483647'  # gRPC 使用的 int32 上限，约 24.8 天
os.environ['GRPC_KEEPALIVE_TIME_MS'] = _GRPC_MAX_MS
os.environ['GRPC_KEEPALIVE_TIMEOUT_MS'] = '20000'
os.environ['GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS'] = '0'
os.environ['GRPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS'] = _GRPC_MAX_MS
os.environ['GRPC_HTTP2_MIN_PING_INTERVAL_WITHOUT_DATA_MS'] = _GRPC_MAX_MS

logger = logging.getLogger(__name__)

try:
    from pymilvus import MilvusClient
    from sentence_transformers import SentenceTransformer
    DEPENDENCIES_AVAILABLE = True
except ImportError as e:
    logger.warning(f"RAG dependencies not available: {e}")
    logger.warning("Install with: pip install pymilvus sentence-transformers")
    DEPENDENCIES_AVAILABLE = False

# 混合检索（BM25 关键词召回）；缺失时自动退化为纯向量检索
try:
    import jieba
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False

# Rerank 可选依赖：CrossEncoder 加载本地 bge-reranker-v2-m3（与 BM25 同样的优雅降级模式）
try:
    from sentence_transformers import CrossEncoder
    RERANKER_AVAILABLE = True
except ImportError:
    RERANKER_AVAILABLE = False

# 混合检索权重：向量语义相似度 vs BM25 关键词匹配
# 混合检索融合：RRF（Reciprocal Rank Fusion）——只用两路的排名，不用分数
# score = Σ 1/(k + rank)，k=60 为业界通用值（Elasticsearch/LangChain 同款）。
# 相比加权融合的优势：两路分数量纲不同（COSINE 相似度 vs BM25 归一分）不可直接加权，
# RRF 只看排名天然规避；且单路命中的文档也有稳定贡献（1/(k+1)），不会被另一路压死。
RRF_K = 60

# Rerank 配置：RRF 粗排后用交叉编码器精排（query-doc 对逐对打分，比双塔向量更准）。
# 缺模型/依赖时优雅降级——直接用 RRF 排序结果。
RERANKER_MODEL_PATH = "data/models/bge-reranker-v2-m3"
RERANK_CANDIDATES = 20  # 粗排送精排的候选数（精排后截断 top_k）


class RAGKnowledgeAgent(AgentBase):
    """RAG知识库智能体"""

    def __init__(
        self,
        name: str = "RAGKnowledgeAgent",
        model=None,
        knowledge_base_path: str = None,
        collection_name: str = "business_travel_knowledge",
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        top_k: int = 6,
        **kwargs
    ):
        super().__init__()
        self.name = name
        self.model = model
        
        if knowledge_base_path is None:
            # Default to local data directory in skill folder
            current_dir = Path(__file__).parent.parent
            knowledge_base_path = str(current_dir / "data" / "rag_knowledge")

        self.knowledge_base_path = Path(knowledge_base_path)
        self.collection_name = collection_name
        self.top_k = top_k
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

        from utils.travel_knowledge import TravelKnowledge
        supplemental_dir = Path(__file__).resolve().parents[1] / "data" / "chinatravel"
        self._travel_knowledge = TravelKnowledge(supplemental_dir / "knowledge.sqlite")
        self._vector_available = False

        if not DEPENDENCIES_AVAILABLE:
            logger.error("RAG dependencies not installed. Install with: pip install pymilvus sentence-transformers")
            self.initialized = bool(self._travel_knowledge.count)
            if self.initialized:
                logger.warning("Using supplemental travel keyword index; vector retrieval unavailable")
            return

        # 优先使用 config 中的配置（支持本地路径，避免连 HuggingFace）
        try:
            from config import RAG_CONFIG
            embedding_model = RAG_CONFIG.get("embedding_model", embedding_model)
        except Exception:
            pass

        # 若配置的是本地路径且存在，则从本地加载，否则按模型 ID 使用（会联网）
        model_path_or_id = embedding_model
        path_obj = Path(embedding_model).expanduser()
        if not path_obj.is_absolute():
            path_obj = Path.cwd() / path_obj
        if path_obj.exists():
            model_path_or_id = str(path_obj.resolve())
            logger.info(f"Using local embedding model: {model_path_or_id}")
        else:
            if "/" in embedding_model or "\\" in embedding_model or embedding_model.startswith("."):
                logger.warning(
                    f"Configured embedding path does not exist: {embedding_model}，将使用 BAAI/bge-small-zh-v1.5 并尝试联网下载。"
                )
                model_path_or_id = "BAAI/bge-small-zh-v1.5"
        logger.info(f"Loading embedding model: {model_path_or_id}")
        self.embedding_model = SentenceTransformer(model_path_or_id)
        self.embedding_dim = self.embedding_model.get_sentence_embedding_dimension()

        # 初始化 Milvus Lite（本地文件存储）
        milvus_db_path = str(self.knowledge_base_path / "milvus_lite.db")
        logger.info(f"Initializing Milvus Lite at: {milvus_db_path}")

        self.milvus_client = MilvusClient(milvus_db_path, grpc_options={"keepalive_time": _GRPC_MAX_MS, "keepalive_timeout": "20000", "keepalive_permit_without_calls": "0", "http2_min_recv_ping_interval_without_data": _GRPC_MAX_MS, "http2_min_ping_interval_without_data": _GRPC_MAX_MS})
        self._client_created_at = None  # 用于追踪客户端创建时间

        # 检查collection是否存在
        if self.milvus_client.has_collection(collection_name):
            logger.info(f"Loaded existing collection: {collection_name}")
        else:
            # 创建新collection
            logger.info(f"Creating new collection: {collection_name}")
            self.milvus_client.create_collection(
                collection_name=collection_name,
                dimension=self.embedding_dim,
                metric_type="COSINE",  # 余弦相似度
                auto_id=False,
            )
            logger.info(f"Created new collection: {collection_name}")

        self.initialized = True
        self._vector_available = True
        self._milvus_db_path = milvus_db_path  # 保存路径用于重连
        # 混合检索：基于全量 chunk 构建 BM25 索引（语料小，内存索引足够）
        self._bm25 = None
        self._bm25_docs = []
        self._rebuild_bm25_index()
        # Reranker 懒加载（首次检索时才加载 2.1GB 模型，避免启动卡死）
        self._reranker = None
        logger.info("RAG Knowledge Agent (Milvus Lite) initialized successfully")

    def _rebuild_bm25_index(self):
        """从 Milvus 拉取全量 chunk，构建 BM25 索引（jieba 分词）"""
        if not BM25_AVAILABLE or not self.initialized:
            return
        try:
            rows = self.milvus_client.query(
                collection_name=self.collection_name,
                filter='id >= 0',
                output_fields=["id", "content", "metadata"],
            )
            if not rows:
                return
            self._bm25_docs = rows
            tokenized = [list(jieba.cut_for_search(r.get("content", ""))) for r in rows]
            self._bm25 = BM25Okapi(tokenized)
            logger.info(f"BM25 index built over {len(rows)} chunks")
        except Exception as e:
            logger.warning(f"BM25 index build failed, vector-only retrieval: {e}")
            self._bm25 = None

    def _load_reranker(self):
        """懒加载本地 bge-reranker-v2-m3（失败降级 None——rerank 跳过，用 RRF 粗排结果）"""
        if not RERANKER_AVAILABLE:
            return None
        if self._reranker is not None:
            return self._reranker
        import os
        model_path = RERANKER_MODEL_PATH
        if not os.path.isdir(model_path):
            logger.warning(f"Reranker model not found at {model_path}, skip rerank")
            return None
        try:
            from sentence_transformers import CrossEncoder
            self._reranker = CrossEncoder(model_path, max_length=1024)
            logger.info(f"Reranker loaded: {model_path}")
            return self._reranker
        except Exception as e:
            logger.warning(f"Reranker load failed, use RRF ranking: {e}")
            self._reranker = None
            return None

    def _rerank(self, query: str, docs: List[Dict], k: int) -> List[Dict]:
        """CrossEncoder 精排：query 与每个 doc 组对打分，按相关性重排。
        reranker 不可用时直接返回 RRF 粗排的前 k 条（优雅降级）。"""
        reranker = self._load_reranker()
        if reranker is None or not docs:
            return docs[:k]
        try:
            pairs = [(query, d.get("content", "")) for d in docs]
            scores = reranker.predict(pairs)
            for d, sc in zip(docs, scores):
                d['rerank_score'] = float(sc)
            ranked = sorted(docs, key=lambda x: x['rerank_score'], reverse=True)[:k]
            logger.info(f"Reranked {len(docs)} -> {len(ranked)} docs")
            return ranked
        except Exception as e:
            logger.warning(f"Rerank failed, fallback to RRF ranking: {e}")
            return docs[:k]

    def _ensure_connection(self):
        """确保 Milvus 连接正常，如果需要则重新创建客户端"""
        try:
            # 尝试一个轻量级操作来检查连接
            self.milvus_client.has_collection(self.collection_name)
        except Exception as e:
            logger.warning(f"Milvus connection issue detected: {e}, reconnecting...")
            try:
                # 关闭旧连接
                if hasattr(self.milvus_client, 'close'):
                    try:
                        self.milvus_client.close()
                    except:
                        pass

                # 重新创建客户端
                self.milvus_client = MilvusClient(self._milvus_db_path)
                logger.info("Milvus client reconnected successfully")
            except Exception as reconnect_error:
                logger.error(f"Failed to reconnect Milvus: {reconnect_error}")
                raise

    def add_documents(self, documents: List[Dict[str, str]]) -> Dict:
        """
        添加文档到知识库

        Args:
            documents: 文档列表，每个文档包含 {'content': '内容', 'metadata': {...}}

        Returns:
            添加结果统计
        """
        if not self.initialized:
            return {"status": "error", "message": "RAG Agent not initialized"}
        if not self._vector_available:
            return {"status": "error", "message": "Vector dependencies unavailable; supplemental travel index is read-only"}

        try:
            # 确保连接正常
            self._ensure_connection()
            # 获取当前文档总数，用于生成连续的ID
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            current_count = stats.get("row_count", 0)

            # 准备数据
            data_to_insert = []

            for i, doc in enumerate(documents):
                # Milvus 要求 id 必须是 int64
                doc_id = current_count + i + 1
                content = doc['content']
                metadata = doc.get('metadata', {})

                # 生成向量
                embedding = self.embedding_model.encode(content).tolist()

                # Milvus 数据格式
                data_to_insert.append({
                    "id": doc_id,
                    "vector": embedding,
                    "content": content,
                    "metadata": json.dumps(metadata, ensure_ascii=False)  # 将metadata转为JSON字符串
                })

            # 批量插入到 Milvus
            self.milvus_client.insert(
                collection_name=self.collection_name,
                data=data_to_insert
            )

            # 获取总数
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            total_count = stats.get("row_count", len(documents))

            logger.info(f"Successfully added {len(documents)} documents to knowledge base")
            self._rebuild_bm25_index()  # 语料更新后重建 BM25 索引
            return {
                "status": "success",
                "added_count": len(documents),
                "total_count": total_count
            }

        except Exception as e:
            logger.error(f"Error adding documents: {e}")
            return {"status": "error", "message": str(e)}

    def search_knowledge(self, query: str, top_k: Optional[int] = None) -> List[Dict]:
        """
        检索知识库

        Args:
            query: 查询文本
            top_k: 返回top k个结果

        Returns:
            检索结果列表
        """
        if not self.initialized:
            return []

        k = top_k or self.top_k
        travel_hits = self._travel_knowledge.search(query, max(k, RERANK_CANDIDATES))
        if not self._vector_available:
            return travel_hits[:k]

        try:
            # 确保连接正常
            self._ensure_connection()
            k = top_k or self.top_k

            # ---- 混合检索：向量召回（扩大候选池）+ BM25 关键词召回，加权融合 ----
            query_embedding = self.embedding_model.encode(query).tolist()
            candidate_pool = max(k * 2, 12)  # 向量多召回一些，融合后再截断

            results = self.milvus_client.search(
                collection_name=self.collection_name,
                data=[query_embedding],
                limit=candidate_pool,
                output_fields=["id", "content", "metadata"]
            )

            # 解析向量召回结果：id -> {content, metadata, similarity}
            candidates: Dict[str, Dict] = {}
            if results and len(results) > 0:
                for hit in results[0]:
                    metadata_str = hit.get("entity", {}).get("metadata", "{}")
                    try:
                        metadata = json.loads(metadata_str)
                    except Exception:
                        metadata = {}
                    doc_id = metadata.get("knowledge_id") or hit.get("entity", {}).get("id", "")
                    # COSINE 距离 -> 相似度
                    candidates[doc_id] = {
                        'id': doc_id,
                        'content': hit.get("entity", {}).get("content", ""),
                        'metadata': metadata,
                        # Milvus COSINE 度量返回的 "distance" 实为相似度（降序，越大越相似）
                        'vector_sim': hit.get("distance", 0.0),
                    }

            # 向量路排名（Milvus 按相似度降序返回，名次即顺序）
            vector_rank: Dict[str, int] = {}
            for rank, doc_id in enumerate(
                    sorted(candidates, key=lambda d: candidates[d].get('vector_sim', 0.0), reverse=True)
                    if candidates else []):
                if candidates[doc_id].get('vector_sim', 0.0) > 0:
                    vector_rank[doc_id] = rank + 1  # 1-based

            # BM25 打分（补充向量漏掉的精确关键词命中；含 candidates 之外的文档）
            bm25_rank: Dict[str, int] = {}
            if self._bm25 is not None and self._bm25_docs:
                query_tokens = list(jieba.cut_for_search(query))
                scores = self._bm25.get_scores(query_tokens)
                # 有正分的文档按分数降序取排名（1-based）
                hits = [(doc, score) for doc, score in zip(self._bm25_docs, scores) if score > 0]
                hits.sort(key=lambda x: x[1], reverse=True)
                for rank, (doc, _) in enumerate(hits):
                    try:
                        doc_metadata = json.loads(doc.get("metadata", "{}"))
                    except (ValueError, TypeError):
                        doc_metadata = {}
                    doc_id = doc_metadata.get("knowledge_id") or doc.get("id", "")
                    bm25_rank[doc_id] = rank + 1
                    if doc_id not in candidates:
                        # BM25 独有命中：从元数据补全 content/metadata
                        metadata_str = doc.get("metadata", "{}")
                        try:
                            metadata = json.loads(metadata_str)
                        except Exception:
                            metadata = {}
                        candidates[doc_id] = {
                            'id': doc_id,
                            'content': doc.get("content", ""),
                            'metadata': metadata,
                            'vector_sim': 0.0,
                        }

            travel_rank = {}
            for rank, doc in enumerate(travel_hits, 1):
                travel_rank[doc['id']] = rank
                candidates.setdefault(doc['id'], doc)

            # RRF 融合：score = Σ 1/(RRF_K + rank_i)，两路排名互相独立
            for c in candidates.values():
                doc_id = c.get('id', '')
                c['score'] = sum(
                    1.0 / (RRF_K + r) for r in (vector_rank.get(doc_id), bm25_rank.get(doc_id), travel_rank.get(doc_id)) if r
                )

            # ---- 粗排（RRF 融合）：取送精排的候选池 ----
            rough = sorted(candidates.values(), key=lambda x: x['score'], reverse=True)[:max(k, RERANK_CANDIDATES)]

            # ---- 精排（Rerank）：CrossEncoder 对 query-doc 对逐对打分，重排后截断 top_k ----
            retrieved_docs = self._rerank(query, rough, k)

            # 兼容旧返回结构：distance 字段（沿 Milvus COSINE 语义 = 相似度）
            for c in retrieved_docs:
                c['distance'] = c['score']

            logger.info(f"Hybrid retrieved {len(retrieved_docs)} documents for query: {query[:50]}")
            return retrieved_docs

        except Exception as e:
            logger.error(f"Error searching knowledge: {e}")
            return travel_hits[:k]

    async def _llm_text(self, prompt: str) -> str:
        """调 LLM 返回纯文本（兼容异步生成器 / 普通响应），失败抛异常"""
        messages = [
            {"role": "system", "content": "你是一个商旅知识专家。"},
            {"role": "user", "content": prompt}
        ]
        response = await self.model(messages)
        answer = ""
        if hasattr(response, '__aiter__'):
            async for chunk in response:
                if isinstance(chunk, str):
                    answer = chunk
                elif hasattr(chunk, 'content'):
                    if isinstance(chunk.content, str):
                        answer = chunk.content
                    elif isinstance(chunk.content, list):
                        for item in chunk.content:
                            if isinstance(item, dict) and item.get('type') == 'text':
                                answer = item.get('text', '')
        elif hasattr(response, 'text'):
            answer = response.text
        elif hasattr(response, 'content'):
            answer = response.content
        elif isinstance(response, dict) and 'content' in response:
            answer = response['content']
        else:
            answer = str(response) if response else ""
        # 清理 LLM 可能输出的 JSON 格式
        answer_str = answer.strip()
        if answer_str.startswith("{") and answer_str.endswith("}"):
            try:
                json_obj = json.loads(answer_str)
                if isinstance(json_obj, dict):
                    answer = json_obj.get("answer") or json_obj.get("content") or answer
            except Exception:
                pass
        return answer.strip()

    @staticmethod
    def _insufficient(answer: str) -> bool:
        """判定 LLM 答案是否「知识库信息不足」（与提示词强约束的措辞对齐）"""
        if not answer:
            return True
        markers = ("没有找到", "未找到", "未提及", "知识库中没有", "没有相关知识")
        return any(m in answer for m in markers)

    async def _rewrite_query(self, user_query: str) -> Optional[str]:
        """补检索用：让 LLM 生成一个改写后的检索查询（只输出查询本身）。失败返回 None。"""
        try:
            prompt = (f"用户问题：{user_query}\n\n"
                      f"刚才用这个问题在知识库检索，未命中或信息不足。"
                      f"请生成一个改写后的检索查询：换关键词、换同义词、或换更具体的表述。"
                      f"只输出查询本身，不要任何解释和标点符号以外的内容。")
            rewritten = await self._llm_text(prompt)
            rewritten = rewritten.strip().strip('"').strip("'")
            if rewritten and rewritten != user_query and len(rewritten) < 200:
                return rewritten
        except Exception as e:
            logger.warning(f"Query rewrite failed: {e}")
        return None

    async def _generate_answer(self, user_query: str, retrieved_docs: List[Dict]) -> str:
        """基于检索片段生成答案（无 LLM 时直接返回知识原文）"""
        knowledge_context = "\n\n".join([
            f"【知识片段{i+1}】\n{doc['content']}"
            for i, doc in enumerate(retrieved_docs)
        ])
        if not self.model:
            return "以下是知识库中的相关信息：\n\n" + knowledge_context

        skill_instruction = self.skill_loader.get_skill_content("ask-question")
        if not skill_instruction:
            skill_instruction = "请基于知识库中的信息回答用户的问题。"

        prompt = f"""你是一个商旅知识专家。请严格基于以下知识库中的信息回答用户的问题。

【用户问题】
{user_query}

【知识库信息】
{knowledge_context}

【任务说明】
{skill_instruction}

【重要约束】
1. 如果【知识库信息】中没有包含回答用户问题所需的信息，请直接回答“抱歉，知识库中没有找到相关信息”，不要尝试根据你自己的知识编造答案。
2. 即使问题很基础，如果知识库里没写，就说不知道。
3. 请以专业、客观的语气回答。
4. 标注为ChinaTravel静态测评沙盒的内容仅供演示。引用其价格、时间或设施时，必须说明为静态参考数据、出行前需核实，不得称为实时查询结果；它不构成企业报销或差旅标准。
"""
        try:
            answer = await self._llm_text(prompt)
            return answer or "无法生成答案"
        except Exception as e:
            logger.error(f"Error generating answer with LLM: {e}")
            return f"知识库中找到相关信息，但生成答案时出错：{str(e)}"

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        RAG问答主流程
        1. 接收用户查询
        2. 检索相关知识
        3. 结合知识生成答案
        4. 有界补检索（M4）：检索未命中或答案自报信息不足时，
           改写查询重检索一次（上限 1 轮），命中则合并片段重新生成
        """
        if not self.initialized:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "error",
                    "message": "RAG Agent not initialized. Please install dependencies: pip install pymilvus sentence-transformers"
                }),
                role="assistant"
            )

        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 获取用户查询
        if isinstance(x, list):
            content = x[-1].content if x else ""
        else:
            content = x.content

        # 尝试解析 JSON 输入 (来自 Orchestrator)
        user_query = content
        if isinstance(content, str) and content.strip().startswith('{'):
            try:
                data = json.loads(content)
                # 只要解析成功，就认为 content 是结构化数据，尝试提取 query
                extracted_query = ""
                if "context" in data and isinstance(data["context"], dict):
                    extracted_query = data["context"].get("rewritten_query", "")
                elif "rewritten_query" in data:
                    extracted_query = data.get("rewritten_query", "")

                # 使用提取到的 query（即使为空，也比 JSON 字符串好）
                user_query = extracted_query
            except:
                pass  # 解析失败则保留原字符串

        # 边界治理（M5）：超长输入直接拒绝，不烧 LLM
        if len(user_query or "") > 20_000:
            return Msg(name=self.name, content=json.dumps({
                "status": "error",
                "message": f"查询超长（{len(user_query)} 字符），已拒绝执行",
            }, ensure_ascii=False), role="assistant")

        # 检索相关知识
        retrieved_docs = self.search_knowledge(user_query)

        # ---------- 有界补检索（M4）：第一轮 ----------
        answer = None
        supplemental = None
        if retrieved_docs:
            answer = await self._generate_answer(user_query, retrieved_docs)

        need_retry = (not retrieved_docs) or self._insufficient(answer)
        if need_retry and self.model:
            rewritten = await self._rewrite_query(user_query)
            if rewritten:
                extra_docs = self.search_knowledge(rewritten)
                # 去重合并（按内容前缀粗判重）
                seen = {d['content'][:120] for d in retrieved_docs}
                new_docs = [d for d in extra_docs if d['content'][:120] not in seen]
                if new_docs:
                    logger.info(f"Supplemental retrieval '{rewritten}' hit {len(new_docs)} new docs")
                    retrieved_docs = retrieved_docs + new_docs
                    answer = await self._generate_answer(user_query, retrieved_docs)
                    supplemental = {"queries": [user_query, rewritten],
                                    "added_documents": len(new_docs)}
                else:
                    supplemental = {"queries": [user_query, rewritten],
                                    "added_documents": 0}
                    if answer is None:
                        # 原查询零命中且补检索也无新货 → 维持 no_knowledge
                        answer = None

        if not retrieved_docs or answer is None:
            result = {
                "status": "no_knowledge",
                "query": user_query,
                "answer": "抱歉，我在知识库中没有找到相关信息。",
                "retrieved_documents": [],
                **({"supplemental_retrieval": supplemental} if supplemental else {})
            }
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        result = {
            "status": "success",
            "query": user_query,
            "answer": answer,
            "retrieved_documents": [
                {
                    "content": doc['content'][:200] + "..." if len(doc['content']) > 200 else doc['content'],
                    "metadata": doc['metadata']
                }
                for doc in retrieved_docs
            ]
        }
        if supplemental:
            result["supplemental_retrieval"] = supplemental

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    def get_stats(self) -> Dict:
        """获取知识库统计信息"""
        if not self.initialized:
            return {"status": "error", "message": "Not initialized"}
        if not self._vector_available:
            return {"status": "success", "retrieval_mode": "supplemental_keyword_only",
                    "total_documents": self._travel_knowledge.count,
                    "knowledge_base_path": str(self._travel_knowledge.path)}

        try:
            # 确保连接正常
            self._ensure_connection()
            stats = self.milvus_client.get_collection_stats(self.collection_name)
            return {
                "status": "success",
                "collection_name": self.collection_name,
                "total_documents": stats.get("row_count", 0),
                "supplemental_documents": self._travel_knowledge.count,
                "knowledge_base_path": str(self.knowledge_base_path)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def close(self):
        """关闭 Milvus 连接"""
        if hasattr(self, 'milvus_client'):
            try:
                if hasattr(self.milvus_client, 'close'):
                    self.milvus_client.close()
                    logger.info("Milvus client closed successfully")
            except Exception as e:
                logger.warning(f"Error closing Milvus client: {e}")

    def __del__(self):
        """析构函数，确保资源被释放"""
        self.close()
