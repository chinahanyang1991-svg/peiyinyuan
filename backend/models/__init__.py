"""模型包统一注册入口。

历史现状(F-R4-3/DB-SCHEMA 用例2 记录): 本文件曾为空, 模型注册靠各调用点
自行 import, 导致 `_ensure_all_tables`/`init_db` 的清单一旦漏掉某模块,
全新安装就会缺表(实测漏过 memory -> 缺 memories 表)。

现在把全部模型模块在此集中 import, 任何 `import backend.models.*` 的路径都会
注册 Base.metadata 里的全部表; base.py 两份启动清单仍逐一列出, 由
qa/harness/run_db_schema.py 用例2(静态清单对账 + 子进程行为证明)双重看护。
新增模型模块时: 1) 在这里加一行; 2) 在 base.py 的两份清单里加一行。
"""

from backend.models.book import Book, Chapter  # noqa: F401
from backend.models.memory import Memory  # noqa: F401
from backend.models.project import DubbingProject, Role, VoiceAssignment  # noqa: F401
from backend.models.task import ConversionTask  # noqa: F401
from backend.models.voice import VoiceProfile  # noqa: F401
from backend.models.workflow import WorkflowTemplate  # noqa: F401

__all__ = [
    "Book", "Chapter",
    "Memory",
    "DubbingProject", "Role", "VoiceAssignment",
    "ConversionTask",
    "VoiceProfile",
    "WorkflowTemplate",
]
