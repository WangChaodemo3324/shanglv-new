"""Process-wide audit guard. Run only in the dedicated test subprocess."""
import os
import sys
import traceback
from pathlib import Path
from urllib.parse import urlparse, unquote


class TestIsolationViolation(RuntimeError):
    pass


class Guard:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.violations = []
        self.details = []

    def deny(self, event):
        self.violations.append(event)
        self.details.append({"event": event, "stack": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_stack(limit=18)]})
        raise TestIsolationViolation(event)

    def inside(self, path):
        if isinstance(path, int):
            return True  # already-open stdout/report handles
        if str(path).startswith("file:"):
            path = unquote(urlparse(str(path)).path)
            if os.name == "nt" and path.startswith("/"):
                path = path[1:]
        return Path(os.fsdecode(path)).resolve().is_relative_to(self.root)

    def audit(self, event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.bind",
                     "socket.getaddrinfo", "socket.sendto", "subprocess.Popen",
                     "os.system", "os.startfile", "sqlite3.connect"}:
            if event == "sqlite3.connect" and (args[0] == ":memory:" or self.inside(args[0])):
                return
            self.deny(event)
        if event == "open":
            path, mode, flags = args
            writing = (mode and any(c in mode for c in "wax+")) or (
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not self.inside(path):
                self.deny("write outside test root")
            if not writing and not isinstance(path, int):
                resolved = Path(os.fsdecode(path)).resolve()
                project = Path(__file__).resolve().parents[2]
                protected = (project / "data", project / "skills/ask-question/data/rag_knowledge",
                             project / "skills/ask-question/data/chinatravel")
                if resolved == project / "config.py" or any(resolved.is_relative_to(p) for p in protected):
                    self.deny("read production config or storage")
        indexes = {"os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,),
                   "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (1,),
                   "os.chmod": (0,), "os.truncate": (0,)}
        for index in indexes.get(event, ()):
            if not self.inside(args[index]):
                self.deny(event + " outside test root")

    def install(self):
        sys.addaudithook(self.audit)
