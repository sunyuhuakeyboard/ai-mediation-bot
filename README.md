# AI 电话调解机器人（低时延 · 知识驱动 · 合规优先）

基于《AI话术应对策略》技术方案与《资料准备字段模板.xlsx》知识库实现的完整后端工程：
**FastAPI + Redis（会话状态/热更新）+ PostgreSQL（知识库/运行数据）**。

核心理念（与方案一致）：**流程、合规、关键节点不交给大模型**——对话由 SOP 节点表 +
决策路由表驱动，话术优先模板直出，LLM 仅在少数路由上生成一句 ≤30 字的过渡短语，
且输出仍要过合规规则引擎；金额/时间/期数的复述确认强制模板渲染，零幻觉。

## 一、单回合处理链路

```
ASR最终文本
  → 意图/抗性分类（小模型HTTP 250ms预算，超时降级关键词；极短"嗯/对/不是"按节点极性映射）
  → 决策路由表（内存索引，(节点,意图,槽位条件,置信度,优先级) 匹配，<1ms）
  → 槽位写入（金额/时间/期数/回访时间抽取 + 标签效应槽 + 路由set_slots）
  → 动作执行
      DIRECT_TEMPLATE   模板直出（占绝大多数回合，0 LLM调用）
      LLM_SHORT_REPLY   流式生成，首句截断即返回；1.5s超时回退参考话术
      ASK_SLOT/门控     方案要素不齐自动追问（分几期/每期多少/什么时候）
      TRANSFER_HUMAN / END_CALL / FALLBACK重试与兜底跳转
  → 节点链式衔接（陈述型节点自动拼接下一主问句，一次播报）
  → 合规规则引擎（CR001~CR010 整段校验，命中即替换为预审修复话术）
  → TTS文本（已清洗）；状态写Redis（1次RTT）；PG落库走后台任务，不占回合延迟
```

### 时延设计要点（对应方案性能指标）

| 环节 | 目标 | 实现手段 |
|---|---|---|
| 意图分类 | ≤250ms | 小模型HTTP带超时熔断，失败即关键词规则（µs级） |
| 决策路由 | <10ms | 知识全量内存快照 + (节点,意图)字典索引，实测µs级 |
| 模板直出 | <5ms | 变量渲染纯字符串操作；金额格式化去浮点尾巴 |
| LLM短句 | 300~800ms首字 | stream=True + 首个句末标点即截断；max_tokens=60 |
| 超时兜底 | 1.5s硬上限 | asyncio.timeout，超时返回参考话术，绝不沉默 |
| 落库 | 0ms（异步） | asyncio.create_task 后台写PG，回合只写1次Redis |
| 知识更新 | 秒级、不重启 | PG维护 → /admin/knowledge/reload → Redis pubsub广播全副本 |

离线冒烟实测：模板直出回合端到端 <1ms（不含网络/ASR/TTS）。

## 二、与交付资料的对应关系

| Excel Sheet | 数据表 | 代码 |
|---|---|---|
| 02_案件字段表 | cases | `app/db/models.py::Case`，导入接口支持中文表头 |
| 03_SOP节点表 N001~N025 | sop_nodes | `app/knowledge/seed.py::NODES`（含节点主问句/链式衔接标记） |
| 04_意图抗性标签表 | intent_labels | `seed.py::LABELS` + 极性映射 `NODE_POLAR_MAP` |
| 05_决策路由表 R001~R020 | decision_routes | `seed.py::ROUTES`（原表20条 + 系统补充R1xx打通全链路） |
| 06_调解策略库 | strategies | `seed.py::STRATEGIES` |
| 07_话术模板库 | script_templates | `seed.py::TEMPLATES`（原表19条 + 主问句/追问槽位等补充） |
| 08_Prompt组件库 | prompt_components | `seed.py::COMPONENTS`，拼装逻辑 `engines/prompt_builder.py` |
| 09_合规规则库 CR001~CR010 | compliance_rules | `engines/compliance.py`（CR001/CR005为动态隐私规则） |
| 10_通话质检规则 QC001~QC010 | qc_rules | `services/quality_service.py` |
| 12_运行示例 | — | `tests/test_dialog_flow.py` 完整复现该对话 |

> 所有 `remark="系统补充"` 的路由/话术/标签是为打通完整对话闭环新增的，
> 业务侧可在 Excel/数据库中按同样格式继续维护。

## 二之二、商用化增强（v1.1）

**合规告知**：开场白自动追加录音告知与AI身份披露（`OPENING_DISCLOSURE`，默认开启，不建议关闭）。

**外呼策略层**（`OUTBOUND_POLICY_ENABLED`）：勿扰时段（默认21:00-08:00）、单号码当日呼叫上限（默认2次）、DNC谢绝名单三道闸，违规返回409；用户说"别再打/不要再打"自动入DNC，后续外呼强制拦截；`force=true` 供人工强呼/呼入场景跳过。DNC运营接口：`GET/POST/DELETE /admin/dnc[/{phone}]`。

**否定语境检测**："我不可能下个月还1000"不再被误判为承诺方案——否定词与金额同小句时丢弃方案槽位并归类为无力还款；让步+承诺（"拿不出太多，下个月还1000吧"）不受影响。

**话术变体池轮换（防复读机的首选形态）**：同一模板可挂多条人审同义变体（`variants`字段），运行时按通话顺序轮换、本通不重复——多样性由离线生成+人工审核保证，运行时零LLM延迟、合规100%预审。`scripts/generate_variants.py` 用LLM批量生成变体草稿（输出draft文件供人审，绝不自动入库）。实时LLM退居两类场景：策略性共情改写、UNKNOWN长尾。

**LLM动态提示词升级**：新增对话历史（近4条）、已知信息（已确认本人/意愿/已报方案要素，禁止重复询问）、防重复（最近说过的话，要求换措辞）三个组件，解决跨轮一致性与复读感；`_LLM_COMPS` 路由默认携带。

**UNKNOWN长尾受限应答**：路由未命中时先让LLM"简短回应+拉回当前节点问题"（仍过合规引擎），失败才退"没听清"重试——告别只会复读的兜底。

**ASR碎片合并**：上一轮未命中的短文本与本轮拼接后重试一次路由（"我想分"+"期还"→正确识别分期诉求）。

**金额合理性复核**：承诺金额超过欠款总额×1.5或小于1元（典型ASR误识别），清槽并请用户复述，绝不带病进入方案确认；打 `AMOUNT_ANOMALY` 风险标记。

**知识引用完整性校验**：导入Excel后自动校验路由→节点/话术/策略/组件引用，`--strict` 模式发现问题即回滚；快照加载时同样告警。

**可观测性**：`GET /metrics`（Prometheus）暴露回合数、延迟直方图、合规拦截、LLM成败、外呼拦截原因。

**LLM深度质检**（可选，`LLM_AUDIT_ENABLED`）：通话结束后用LLM复查机器人全部发言，捕捉规则引擎漏报的同义改写违规（"今天必须处理掉"类），命中计入QC006并标人工复核。


## 三、快速开始

### 方式A：离线演示（零依赖，30秒跑通）

```bash
pip install -r requirements.txt
OFFLINE_MODE=1 uvicorn app.main:app --port 8000
```

```bash
# 发起通话（使用内置演示案件 张三/橘子分期/16000元）
curl -s -X POST localhost:8000/api/v1/dialog/start \
  -H 'Content-Type: application/json' -d '{"case_id":"CASE20260610001"}'
# → {"call_id":"CALL...","opening":{"reply":"您好，我这边是XX民商事调解中心工作人员...请问您是张三本人吗？"}}

curl -s -X POST localhost:8000/api/v1/dialog/turn \
  -H 'Content-Type: application/json' -d '{"call_id":"CALL...","text":"是我"}'
# → 确认本人后才披露平台/委托方，并自动衔接"是否收到通知"

curl -s -X POST localhost:8000/api/v1/calls/CALL.../end -d '{}'
# → 自动生成质检报告（QC001~QC010 扣分制）
```

### 方式B：Docker 完整部署（PG + Redis + App）

```bash
cp .env.example .env          # 填入 LLM_API_KEY（豆包/Qwen/DeepSeek 任一OpenAI兼容端点）
docker compose up -d --build
```

### 前端控制台

服务启动后直接访问：

```text
http://服务器IP:8000/console/
```

控制台随 FastAPI 一起部署，无需 Node/npm 构建。可在页面内完成健康检查、发起测试通话、
发送单轮用户文本、查看通话状态/转写/质检、查询和导入案件、查看/热更新知识库、
维护 DNC 谢绝名单、OKCTI SSE 接口联调，以及读取 Prometheus 指标。

调试入口：

```text
http://服务器IP:8000/docs      # OpenAPI 文档
http://服务器IP:8000/healthz   # 服务健康
http://服务器IP:8000/metrics   # Prometheus 指标
```

启动时自动建表；知识表为空会自动灌入内置种子（Excel镜像），也可手动：

```bash
python scripts/seed_db.py                              # 灌种子
python scripts/import_knowledge_xlsx.py 模板.xlsx       # 从业务Excel导入/更新
curl -X POST localhost:8000/api/v1/admin/knowledge/reload   # 秒级热生效
```

### OKCTI / LLM-IVR SSE 外呼平台对接

给 OKCTI 配置业务平台 URL：

```text
http://服务器IP:8000/ivr/okcti/welcome
```

同一个接口也提供版本化调试路径：

```text
http://服务器IP:8000/api/v1/ivr/okcti/welcome
```

如 OKCTI 平台固定追加 `/stream`，服务端也兼容以下路径：

```text
http://服务器IP:8000/ivr/okcti/welcome/stream
http://服务器IP:8000/api/v1/ivr/okcti/welcome/stream
```

接口协议：

- 请求：`POST` JSON，字段兼容 OKCTI 文档中的 `callid/caller/callee/direct/type/usrtype/usrcontent/...`
- 响应：`text/event-stream; charset=UTF-8`
- 事件：`wait`、`ivr`、`msg`
- 每个完整消息以 `[E-N=D]` 结尾

最小 START 请求：

```bash
curl -N -X POST localhost:8000/ivr/okcti/welcome \
  -H 'Content-Type: application/json' \
  -d '{
    "callid":"OKCTI_TEST_001",
    "caller":"95000000",
    "callee":"13900000000",
    "direct":1,
    "type":"START",
    "usrtype":0,
    "usrcontent":"",
    "sysid":1,
    "taskid":"TASK_DEMO",
    "calltaskid":"CASE20260610001",
    "video":false
  }'
```

用户说话后的 QA 请求：

```bash
curl -N -X POST localhost:8000/ivr/okcti/welcome \
  -H 'Content-Type: application/json' \
  -d '{
    "callid":"OKCTI_TEST_001",
    "caller":"95000000",
    "callee":"13900000000",
    "direct":1,
    "type":"QA",
    "usrtype":2,
    "usrcontent":"是我，什么事",
    "sysid":1,
    "taskid":"TASK_DEMO",
    "calltaskid":"CASE20260610001",
    "video":false
  }'
```

结束通话：

```bash
curl -N -X POST localhost:8000/ivr/okcti/welcome \
  -H 'Content-Type: application/json' \
  -d '{
    "callid":"OKCTI_TEST_001",
    "caller":"95000000",
    "callee":"13900000000",
    "direct":1,
    "type":"END",
    "usrtype":0,
    "usrcontent":"",
    "sysid":1,
    "talktimelong":60,
    "callresult":1,
    "video":false
  }'
```

推荐生产配置：

```env
OKCTI_AUTH_ENABLED=1
OKCTI_APP_ID=双方约定AppId
OKCTI_APP_SECRET=双方约定密码
OKCTI_RESPONSE_CHARSET=UTF-8
OKCTI_FORCE_START=1
OKCTI_TRANSFER_SKILL=人工坐席
```

如果需要同时保存 OKCTI 公司、分机和 WebRTC SDK 配置，可写入服务器 `.env`：

```env
CTI_COMPANY_ID=56
CTI_EXTENSION_90001_NUMBER=1083
CTI_EXTENSION_90001_PASSWORD=******
CTI_EXTENSION_90002_NUMBER=1084
CTI_EXTENSION_90002_PASSWORD=******
VITE_WS_CTI_URL=wss://v8.iokcall.com/cti
VITE_APP_WEBRTC=true
VITE_APP_WEBRTC_SIP=117.29.161.214
VITE_APP_WEBRTC_PORT=31091
VITE_APP_WEBRTC_WSS=wss://v8ljx.iokcall.com:31743
VITE_APP_WEBRTC_DEBUG=false
VITE_APP_WEBRTC_STUN=
```

`.env` 已被 `.gitignore` 忽略，分机密码、LLM Key、OKCTI Secret 等生产密钥不要提交到仓库。

签名规则按 OKCTI 文档：`MD5(App ID;时间戳;密码;请求ID)`，请求头为
`X-request-Id`、`X-App-Id`、`X-Timestamp`、`X-Sign`。

案件绑定建议：外呼平台在请求体中传 `calltaskid` 或 `case_id` 对应本系统案件编号；
如果没有传，本服务会用 `callid` 和主被叫号码生成最小案件，便于真实电话冒烟测试。

### 运行测试

```bash
python -m pytest tests/ -v        # 端到端用例，离线运行
```

## 四、接口一览（/api/v1）

| 接口 | 说明 |
|---|---|
| `POST /dialog/start` | 发起通话，返回 call_id + 开场白（{case_id} 或内联 {case}） |
| `POST /dialog/turn` | 单回合：ASR文本进，话术出（含路由/槽位/合规/延迟打点） |
| `WS /dialog/ws` | 电话网关长连接：start / user_text / **barge_in（抢话取消在途生成）** / end |
| `POST /calls/{id}/end` | 结束并出质检报告 |
| `GET /calls/{id}/transcript` · `/quality` · `/state` | 转写 / 质检 / 实时状态 |
| `POST /cases/import` | 案件批量导入（支持02表中文表头） |
| `GET /admin/knowledge/{table}` | 查看快照中的节点/标签/路由/话术/策略/合规/质检 |
| `PUT /admin/knowledge/{table}/{pk}` | 单条维护（话术/路由/策略/标签/节点） |
| `POST /admin/knowledge/reload` | 重建快照并广播全副本 |
| `POST /ivr/okcti/welcome` | OKCTI LLM-IVR SSE 兼容入口（root路径） |
| `POST /ivr/okcti/welcome/stream` | OKCTI LLM-IVR SSE 兼容入口（兼容平台 stream 后缀） |
| `POST /api/v1/ivr/okcti/welcome` | OKCTI LLM-IVR SSE 兼容入口（版本化路径） |
| `POST /api/v1/ivr/okcti/welcome/stream` | OKCTI LLM-IVR SSE 兼容入口（版本化 stream 后缀） |

WebSocket 协议示例：

```json
→ {"event":"start","case_id":"CASE20260610001"}
← {"event":"bot_reply","reply":"您好，我这边是XX民商事调解中心...","call_id":"..."}
→ {"event":"user_text","text":"是我"}
← {"event":"bot_reply","reply":"好的，这边是受...委托...","node_after":"N007","latency_ms":{...}}
→ {"event":"barge_in"}        // 用户抢话：取消在途LLM生成
→ {"event":"end"}
```

## 五、对接说明

- **ASR/TTS**：本服务消费"ASR最终文本"、产出"TTS待播文本（segments分句）"。
  与 Fun-ASR/Volcano/讯飞等的媒体流对接放在电话网关侧，通过 WS 协议接入即可；
  `segments` 可逐句送 TTS 实现边播边等。
- **LLM**：任何 OpenAI 兼容端点（豆包方舟 / DashScope-Qwen / DeepSeek / vLLM自部署），
  改 `.env` 三个变量即可。未配置 API Key 时全部路由自动退化为模板直出，链路依然完整。
- **小模型分类**：可选。提供 `POST {CLASSIFIER_URL}` 接口
  （入参 current_node/user_text/history，出参 intent/confidence/slots）即可接入，
  超时 250ms 自动降级关键词分类。
- **多副本**：状态在 Redis、知识热更新走 pubsub，App 无状态可水平扩容。

## 六、目录结构

```
app/
├── config.py                 # 全部可调参数（.env）
├── main.py                   # lifespan装配：离线/数据库双模式
├── db/        models.py postgres.py redis_client.py
├── knowledge/ seed.py        # Excel知识库镜像 + 系统补充（单一事实来源）
├── cache/     knowledge_cache.py   # 内存快照 + pubsub热更新
├── engines/
│   ├── classifier.py         # 小模型→关键词降级 + 槽位抽取 + 极性映射
│   ├── route_engine.py       # 决策表匹配
│   ├── prompt_builder.py     # Prompt组件拼装（PRIVACY/CASE_SAFE按身份态启停）
│   ├── llm_client.py         # 流式首句截断 + 超时回退
│   ├── compliance.py         # CR001~CR010（动态隐私 + 静态正则）
│   ├── call_state.py         # Redis会话状态
│   └── orchestrator.py       # 主编排：路由→动作→链式衔接→合规→状态
├── services/  call_service.py quality_service.py
├── static/    console/       # 后端内置前端控制台（/console/）
└── api/v1/    dialog.py calls.py cases.py admin.py
scripts/   seed_db.py import_knowledge_xlsx.py
tests/     test_dialog_flow.py
```
