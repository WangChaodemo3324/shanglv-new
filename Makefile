.PHONY: help durable dev-api dev-web dev test

help:
	@echo "shanglv 开发入口（启动顺序：pi-durable → api → web）"
	@echo "  make durable   构建并前台启动 pi-durable 服务 (8788)"
	@echo "  make dev-api   启动 FastAPI (8000)；pi-durable 不可用时自动降级本地路径"
	@echo "  make dev-web   启动 web-next (3000)"
	@echo "  make test      Python 单测（pytest）"
	@echo "  make test-node Node 单测（pi-durable 任务模型）"

durable:
	cd services/pi-durable && npm install && npm run build && node dist/index.js

dev-api:
	venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000

dev-web:
	cd web-next && npm run dev

test:
	venv/bin/python -m pytest tests/eval/run_eval.py -q

test-node:
	cd services/pi-durable && npm test
