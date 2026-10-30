"""PanguVerse Web 版：本机单人使用的 Web 界面（零依赖，纯标准库）。

```python
from panguverse.web import serve

serve(port=7788, workspace=".", open_browser=True)
```
"""

from __future__ import annotations

from .api import Response, WebAPI, error_response, json_response
from .jobs import Job, JobManager
from .server import PanguverseHTTPServer, create_server, serve

__all__ = [
    "Job",
    "JobManager",
    "PanguverseHTTPServer",
    "Response",
    "WebAPI",
    "create_server",
    "error_response",
    "json_response",
    "serve",
]
