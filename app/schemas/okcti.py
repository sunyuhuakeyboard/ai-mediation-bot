"""OKCTI LLM-IVR SSE 对接模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OkctiRequest(BaseModel):
    """OKCTI POST 到业务平台的请求体。

    文档中的字段为小写；这里保留 extra 以兼容平台侧扩展字段，例如 case_id/name。
    """

    model_config = ConfigDict(extra="allow")

    callid: str = Field(min_length=1)
    caller: str = ""
    callee: str = ""
    direct: int = 1
    type: str = Field(min_length=1)
    usrtype: int = 0
    usrcontent: str = ""
    usrrecurl: str = ""
    fsx: int | None = None
    ch: int | None = None
    sysid: int | None = None
    taskid: str = ""
    logid: str = ""
    calltaskid: str = ""
    oricaller: str = ""
    video: bool = False
    talktimelong: int | None = None
    callresult: int | None = None

    def extra_value(self, key: str, default: Any = None) -> Any:
        extra = self.model_extra or {}
        return extra.get(key, default)
