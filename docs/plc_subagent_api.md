# 对外API路由总览与接口详细设计

## 这些服务的 ip 端口

http://60.188.37.6:28080/  

## 一、整体说明

### 1. 文档用途

面向**超级智能体开发者**和**外部合作方**，统一规范系统对外API路由、请求/响应、入参出参、权限与使用场景，用于：
1. **设计超级智能体的意图识别和路由分发**
2. **第三方接入集成**

### 2. 快速入门：如何调用系统能力

> **重要**：所有智能体（智能开发、智能测试、形式化验证、智能修复）都共享**同一个入口**，通过 `agent_id` 和 `context` 参数区分。

```json
POST /api/chat/stream

{
  "message": "你的需求描述",
  "agent_id": "选择哪个智能体",      // 一级路由
  "context": {                         // 二级路由
    // 智能体专属配置参数
  }
}
```

**五分钟入门**：
1. 确定用户意图：生成代码？修复错误？验证属性？测试？
2. 选择 `agent_id`：见 [2.2 智能体选择矩阵](#22-一级路由智能体选择矩阵)
3. 配置 `context` 参数：见 [2.4 二级路由配置](#24-二级路由智能体配置参数详解)
4. 参考 [2.5 完整调用示例](#25-完整调用示例) 发送请求

### 3. 分类规则

按**聊天会话、知识库问答、Fuzz测试报告、认证账号、任务/POU、系统健康、智能开发、支付业务**八大模块划分；标注`建议对外`标识，区分**开放接口/场景可选接口/内部私有接口**。

### 4. 通用约定

| 约定项   | 说明                                                         |
| -------- | ------------------------------------------------------------ |
| 请求头   | 所有接口（除健康检查）需携带 `Authorization: Bearer {Token}` 做身份鉴权 |
| 数据格式 | 统一使用 `application/json`                                  |
| 字符编码 | UTF-8                                                        |
| 路径参数 | `{xxx}` 为动态变量，调用时需替换为实际值                     |
| 状态码   | 200成功、401未授权/Token失效、403权限不足、404路由/资源不存在、500服务内部异常 |

### 4. 当前系统中与“对外开放/联调对接”强相关的路由分组

1. 对话与智能体路由
2. 会话管理路由
3. 知识库路由
4. 形式化验证路由
5. Fuzz测试路由
6. 账号认证路由
7. 文件与上传路由
8. 智能开发路由
9. 形式化报告路由
10. 业务扩展路由（支付）
11. 系统健康与案例路由

---

## 二、API路由总览清单

### 一、聊天会话模块

> **模块概述**：聊天会话模块是系统的核心交互中枢，负责处理用户与AI智能体之间的对话交互。该模块统一管理多类型智能体的调度（智能开发、智能修复、形式化验证、智能测试），通过统一的流式/非流式接口对外暴露能力。模块内部维护完整的会话生命周期（创建→运行→完成/中止→历史查询），支持多会话并行、SSE实时推送、会话中断恢复等高级特性。
>
> **核心能力**：统一入口智能路由、流式SSE响应、会话CRUD管理、主动中断控制、完整历史追溯。
>
> **典型场景**：用户输入"帮我生成一个电机启停控制ST程序"，系统通过本模块调度智能开发智能体完成代码生成并流式返回给用户。

| 路由                               | 请求方法 | 接口描述          | 建议对外 |
| ---------------------------------- | -------- | ----------------- | -------- |
| /api/chat/stream                   | POST     | 流式聊天主入口    | 是       |
| /api/chat                          | POST     | 非流式聊天        | 是       |
| /api/models                        | GET      | 获取可用模型列表  | 视场景   |
| /api/history                       | GET      | 获取历史列表      | 视场景   |
| /api/chat/history                  | GET      | 获取聊天历史列表  | 视场景   |
| /api/chat/history                  | POST     | 保存聊天历史      | 视场景   |
| /api/chat/history/item/update      | POST     | 更新历史条目      | 否       |
| /api/message/save                  | POST     | 保存消息          | 否       |
| /api/chat/history                  | DELETE   | 删除聊天历史      | 否       |
| /api/session/delete                | POST     | 删除会话          | 视场景   |
| /api/session/rename                | POST     | 重命名会话        | 视场景   |
| /api/session/abort                 | POST     | 终止内容生成      | 是       |
| /api/session/status/{session_id}   | GET      | 查询会话状态      | 否       |
| /api/session/pin                   | POST     | 会话置顶/取消置顶 | 否       |
| /api/session/share                 | POST     | 分享会话          | 否       |
| /api/shared/{share_id}             | GET      | 查看分享会话      | 否       |
| /api/session/share/{share_id}      | DELETE   | 取消分享          | 否       |
| /api/session/shares                | GET      | 获取分享会话列表  | 否       |
| /api/session/{session_id}/messages | GET      | 获取指定会话消息  | 是       |

### A2. 会话管理相关 (session_routes.py)

> **模块概述**：会话管理子模块专注于会话本身的CRUD操作，与聊天会话模块协同工作。会话是用户与AI交互的上下文容器，每个会话独立维护消息历史、模型配置、状态信息。本模块提供会话级别的元数据管理，包括创建新会话、查询会话列表、获取会话详情、关闭清理会话等原子操作。
>
> **核心能力**：会话创建（支持预置上下文）、会话列表（分页筛选）、会话详情（元数据获取）、会话删除（级联清理）、统计聚合（使用数据分析）。

| 路由                            | 方法   | 说明               | 建议对外 |
| ------------------------------- | ------ | ------------------ | -------- |
| /api/session/create             | POST   | 创建新会话         | 视场景   |
| /api/session/list               | GET    | 会话列表           | 视场景   |
| /api/session/{session_id}       | GET    | 获取会话详情       | 视场景   |
| /api/session/{session_id}/abort | POST   | 中止会话(路径参数) | 是       |
| /api/session/{session_id}       | DELETE | 关闭并清理会话     | 视场景   |
| /api/session/stats              | GET    | 会话统计信息       | 否       |

### 二、知识库问答模块

> **模块概述**：知识库问答模块是系统的RAG（检索增强生成）核心，专注于基于领域知识（PLC编程规范、函数库文档、行业标准）进行精准问答。与通用聊天不同，本模块强调知识溯源——每条回答都应标注引用来源。模块支持多知识库隔离（不同项目/客户使用独立知识库）、知识条目管理（上传、预处理、切分、删除）、知识质量评估等高级特性。
>
> **核心能力**：多知识库隔离管理、文档自动导入预处理、RAG检索问答（带引用溯源）、检索质量诊断评分、外部网络搜索增强。
>
> **典型场景**：用户询问"IEC 61131-3标准中关于功能块继承的规范"，系统从知识库检索相关标准文档片段，结合LLM生成准确回答并标注来源。

| 路由                                               | 请求方法 | 接口描述             | 建议对外 |
| -------------------------------------------------- | -------- | -------------------- | -------- |
| /api/knowledge/qa/sessions/{session_id}/clear      | POST     | 清空问答会话         | 是       |
| /api/knowledge/chat/stream                         | POST     | 知识库流式问答       | 是       |
| /api/knowledge/chat/enhanced                       | POST     | 增强型知识问答       | 是       |
| /api/knowledge/chat/sessions                       | GET      | 获取知识聊天会话列表 | 是       |
| /api/knowledge/chat/sessions/{session_id}          | GET      | 获取知识聊天会话详情 | 是       |
| /api/knowledge/chat/sessions/{session_id}/messages | GET      | 获取知识聊天会话消息 | 是       |
| /api/knowledge/chat/sessions                       | POST     | 创建知识聊天会话     | 是       |
| /api/knowledge/chat/sessions/{session_id}          | DELETE   | 删除知识聊天会话     | 是       |
| /api/knowledge/chat/sessions/{session_id}/clear    | POST     | 清空知识聊天会话     | 是       |
| /api/knowledge/chat/sessions/{session_id}/rename   | PUT      | 重命名知识聊天会话   | 是       |
| /api/knowledge/chat/diagnose                       | GET      | 知识聊天诊断         | 否       |
| /api/knowledge/preprocess-file                     | POST     | 文件预处理           | 视场景   |
| /api/knowledge/batch-items                         | POST     | 批量知识条目操作     | 视场景   |
| /api/knowledge/import-oscat                        | POST     | 导入OSCAT知识        | 否       |
| /api/knowledge/reader/item/{knowledge_id}          | GET      | 知识阅读器条目视图   | 否       |
| /api/knowledge/reader/chunks/{knowledge_id}        | GET      | 知识阅读器分块视图   | 否       |
| /api/knowledge/reader/quality-score                | GET      | 知识质量评分         | 否       |
| /api/knowledge/web-search/status                   | GET      | 网络搜索状态查询     | 视场景   |
| /api/knowledge/web-search                          | GET      | 发起网络搜索         | 视场景   |
| /api/knowledge/chat/intelligent                    | POST     | 智能知识问答         | 是       |
| /api/knowledge/multi-kb/list                       | GET      | 多知识库列表         | 视场景   |
| /api/knowledge/multi-kb/create                     | POST     | 新建知识库           | 视场景   |
| /api/knowledge/multi-kb/switch                     | POST     | 切换默认知识库       | 视场景   |
| /api/knowledge/multi-kb/current                    | GET      | 获取当前默认知识库   | 视场景   |
| /api/knowledge/sync-tasks                          | GET      | 知识同步任务列表     | 否       |

### 三、形式化验证相关

> **模块概述**：形式化验证模块是系统的安全质量保障核心，基于PLCverif引擎对PLC代码进行数学级别的属性验证，发现传统测试无法覆盖的边界条件和并发问题。本模块支持用户以自然语言描述安全需求（如"电机停止时抱闸必须闭合"），系统自动转换为形式化属性并执行验证。验证结果包括属性通过证明、反例轨迹（展示违反属性的具体执行路径）、覆盖范围分析。
>
> **核心能力**：自然语言属性转换（NL→形式化）、输入预校验（避免空跑）、多格式报告输出（JSON/MD/HTML/ZIP）、反例轨迹分析（定位缺陷）、预定义属性模板（降低门槛）。
>
> **典型场景**：用户提交电梯控制代码并输入属性"电梯门关闭时不得移动"，系统验证发现反例轨迹显示存在先移动后关门的不安全路径，提示开发者修复互锁逻辑。

| 路由                                            | 方法 | 说明               | 建议对外 |
| ----------------------------------------------- | ---- | ------------------ | -------- |
| /api/formal-validation/validate                 | POST | 形式化验证输入校验 | 是       |
| /api/formal-validation/convert-natural-language | POST | 自然语言转属性     | 是       |
| /api/formal-validation/format-examples          | GET  | 属性格式示例       | 是       |
| /api/compilation/validate                       | POST | 编译输入校验       | 是       |
| /api/formal/reports/{report_id}.json            | GET  | 形式化报告JSON     | 是       |
| /api/formal/reports/{report_id}.md              | GET  | 形式化报告Markdown | 是       |
| /api/formal/reports/{report_id}.html            | GET  | 形式化报告HTML     | 是       |
| /api/formal/reports/{report_id}/bundle.zip      | GET  | 报告压缩包         | 是       |

### 四、Fuzz测试报告模块

> **模块概述**：Fuzz测试报告模块提供模糊测试结果的查询与下载能力。模糊测试通过自动生成大量测试用例来发现代码缺陷，特别适合PLC控制逻辑中边界条件、异常输入等难以手工覆盖的场景。模块存储历次测试生成的完整报告，支持多格式导出供开发者分析和修复缺陷。
>
> **核心能力**：测试报告多格式导出（Markdown/JSON/ZIP包）、测试用例明细查询（精准复现失败用例）、失败用例上下文记录（辅助根因定位）。
>
> **典型场景**：智能开发生成的代码自动触发Fuzz测试，发现输入值在特定边界条件下输出异常，系统生成详细报告并引导开发者修复。

| 路由                                         | 请求方法 | 接口描述                 | 建议对外 |
| -------------------------------------------- | -------- | ------------------------ | -------- |
| /api/fuzz/reports/{report_id}.md             | GET      | 下载Markdown格式Fuzz报告 | 是       |
| /api/fuzz/reports/{report_id}/bundle.zip     | GET      | 下载Fuzz报告压缩包       | 是       |
| /api/fuzz/reports/{report_id}/testcases.json | GET      | 获取Fuzz测试用例明细     | 是       |

### 五、认证与账号模块

> **模块概述**：认证与账号模块是系统安全的基础，负责用户的身份识别、权限管理、会话控制。本模块采用JWT令牌机制，支持注册登录、密码找回、多端登出等完整用户生命周期管理。同时提供账号信息查询与更新接口，支持用户管理个人资料。
>
> **核心能力**：JWT身份认证（登录/登出/Token刷新）、用户注册与邮箱验证、密码找回（验证码重置）、账号信息管理（查询/更新）、多端会话控制（强制其他设备登出）。
>
> **典型场景**：新用户注册后收到邮箱验证链接，点击验证后激活账号；用户更换设备后通过原Token刷新获取新令牌，无需重新登录。

| 路由                      | 请求方法 | 接口描述        | 建议对外 |
| ------------------------- | -------- | --------------- | -------- |
| /api/auth/login           | POST     | 用户登录        | 是       |
| /api/auth/register        | POST     | 用户注册        | 是       |
| /api/auth/validate        | GET      | Token有效性校验 | 是       |
| /api/auth/check           | GET      | 登录状态检查    | 是       |
| /api/auth/logout          | POST     | 用户登出        | 是       |
| /api/auth/change-password | POST     | 修改密码        | 是       |
| /api/auth/forgot-password | POST     | 忘记密码申请    | 是       |
| /api/auth/verify-code     | POST     | 验证码校验      | 是       |
| /api/auth/reset-password  | POST     | 重置密码        | 是       |
| /api/account/info         | GET      | 获取账号信息    | 是       |
| /api/account/info         | PUT      | 更新账号信息    | 是       |
| /api/user/info            | GET      | 获取用户信息    | 是       |
| /api/user/info            | PUT      | 更新用户信息    | 是       |

### 六、文件相关

> **模块概述**：文件模块负责处理用户上传的PLC代码文件、配置文件及其他二进制资源的存储与访问。本模块提供文件上传、下载、在线预览等基础能力，同时支持语音转文字、POU（编程组织单元）提取与分析等高级功能，为智能开发和知识库提供文件数据支撑。
>
> **核心能力**：单文件/批量文件上传（multipart/form-data）、文件下载与断点续传、在线代码预览（语法高亮）、语音转写（语音输入PLC需求）、POU结构提取与推荐。
>
> **典型场景**：用户上传一个ST源代码文件，系统提取其中的POU结构（函数、功能块），并根据上下文推荐适合的编程组织单元模式。

| 路由                          | 方法 | 说明     | 建议对外 |
| ----------------------------- | ---- | -------- | -------- |
| /api/upload                   | POST | 上传文件 | 是       |
| /api/upload/multiple          | POST | 批量上传 | 是       |
| /api/files/{file_id}/download | GET  | 下载文件 | 是       |
| /api/files/{file_id}/view     | GET  | 在线预览 | 是       |
| /api/voice/transcribe         | POST | 语音转写 | 视场景   |
| /api/pou/recommend            | POST | POU推荐  | 否       |
| /api/pou/extract/{file_id}    | GET  | POU提取  | 否       |

### 七、任务模块

> **模块概述**：任务模块管理系统中长时间运行的异步操作，如代码编译、形式化验证、Fuzz测试等任务。与即时返回的REST接口不同，这些操作需要后台执行并通过任务队列管理。模块提供任务状态查询、进度追踪、结果获取等接口，支持前端轮询或WebSocket推送任务状态。
>
> **核心能力**：异步任务队列管理、任务状态与进度查询、任务结果与日志获取、任务超时与失败处理。

| 路由                 | 请求方法 | 接口描述 | 建议对外 |
| -------------------- | -------- | -------- | -------- |
| /api/tasks           | GET      | 任务列表 | 否       |
| /api/tasks/{task_id} | GET      | 任务详情 | 否       |

### 八、POU模块（内部私有）

> **模块概述**：POU（Program Organization Unit，编程组织单元）模块提供从PLC源文件中解析和提取POU结构的能力。POU是IEC 61131-3标准中的核心概念，包括函数（Function）、功能块（Function Block）和程序（Program）三种类型。本模块支持结构提取、类型识别、依赖分析等高级功能，为代码生成和重构提供底层支撑。
>
> **核心能力**：POU结构提取（函数/功能块/程序识别）、类型推断与声明解析、POU依赖关系分析、智能POU推荐（基于上下文）。

| 路由                       | 请求方法 | 接口描述    | 建议对外 |
| -------------------------- | -------- | ----------- | -------- |
| /api/pou/extract/{file_id} | GET      | POU数据提取 | 否       |

### 九、健康检查模块

> **模块概述**：健康检查模块提供系统运行状态的监控接口，用于负载均衡探测、容器编排（K8s存活/就绪探针）、运维监控等场景。模块分为两层：基础层（`/health`）仅检测进程存活，适合快速探活；全链路层（`/api/health`）检测所有依赖服务（数据库、Redis、外部API）的健康状态，适合深度诊断。
>
> **核心能力**：进程存活检测、数据库连接检测、缓存服务检测、外部API可达性检测、依赖链路诊断报告。

| 路由        | 请求方法 | 接口描述                     | 建议对外 |
| ----------- | -------- | ---------------------------- | -------- |
| /health     | GET      | 基础服务健康检查             | 是       |
| /api/health | GET      | 全链路健康检查（含依赖服务） | 是       |

### 十、系统案例模块

> **模块概述**：系统案例模块提供预置的行业解决方案和典型应用案例，帮助用户快速了解系统能力边界和最佳实践。案例涵盖多种PLC应用场景（过程控制、运动控制、安全系统等），每个案例包含需求描述、解决方案、代码示例和验证结果。用户可通过案例库学习，也可基于案例快速生成定制化代码。
>
> **核心能力**：案例分类浏览与搜索、案例详情与代码示例、案例关联知识推荐、基于案例的代码生成。

| 路由       | 请求方法 | 接口描述     | 建议对外 |
| ---------- | -------- | ------------ | -------- |
| /api/cases | GET      | 业务案例列表 | 视场景   |

### 十一、智能开发模块

> **模块概述**：智能开发模块是系统的代码生成核心，通过自然语言描述自动生成符合IEC 61131-3标准的PLC代码。模块支持多种编程语言（ST、SCL、FBD），内置苏格拉底需求梳理、代码模板、后续管道（测试/验证）等高级特性。与聊天会话模块的 `agent_id` 路由不同，本模块提供独立的能力查询接口，支持前端获取可用语言和模板列表。
>
> **核心能力**：自然语言转PLC代码（ST/SCL/FBD）、苏格拉底需求澄清、代码模板快速初始化、后续管道编排（自动测试/验证）。
>
> **典型场景**：用户输入"帮我写一个PID温度控制器"，系统引导确认控制参数后生成完整的ST代码，并可选触发Fuzz测试验证代码边界行为。

| 路由                           | 请求方法 | 接口描述               | 建议对外 |
| ------------------------------ | -------- | ---------------------- | -------- |
| /api/smart_dev/switch_language | POST     | 切换开发语言           | 是       |
| /api/smart_dev/generate        | POST     | 智能代码/内容生成      | 是       |
| /api/smart_dev/languages       | GET      | 获取支持的开发语言列表 | 是       |
| /api/smart_dev/templates       | GET      | 获取开发模板列表       | 是       |

### 十二、支付业务模块（内部私有）

> **模块概述**：支付业务模块提供商业化所需的订单管理、支付处理和账单查询能力。模块对接主流支付渠道（支付宝、微信支付、银行转账），支持按量计费和订阅制两种计费模式。模块属于内部私有接口，仅供已签约的企业客户使用，普通用户通常无感知。
>
> **核心能力**：支付订单创建与管理、多支付渠道对接、支付结果异步回调处理、账单聚合与明细查询、发票开具（扩展能力）。
>
> **典型场景**：企业用户购买月度订阅，系统创建支付订单，用户完成支付后回调确认订单状态，扣除对应配额。

| 路由                            | 请求方法 | 接口描述           | 建议对外 |
| ------------------------------- | -------- | ------------------ | -------- |
| /api/payment/create-order       | POST     | 创建支付订单       | 否       |
| /api/payment/orders             | GET      | 获取订单列表       | 否       |
| /api/payment/order/{order_id}   | GET      | 获取订单详情       | 否       |
| /api/payment/pay/{order_id}     | POST     | 发起支付           | 否       |
| /api/payment/confirm/{order_id} | POST     | 支付结果确认       | 否       |
| /api/payment/supported-methods  | GET      | 获取支持的支付方式 | 否       |
| /api/payment/billing/list       | GET      | 账单列表查询       | 否       |

---

## 三、超级智能体入口设计指南（必读）

> **设计超级智能体必看**：本章节详细说明如何通过意图识别+二级路由，优雅地调用系统所有能力。

### 2.1 设计理念：统一入口 + 分层配置

系统采用**单一入口 + 参数驱动**的设计模式：

```
┌─────────────────────────────────────────────────────────────────┐
│                    一级路由：选择智能体                            │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌──────────┐  │
│  │  智能开发   │ │  智能修复   │ │ 形式化验证  │ │ 智能测试 │  │
│  │           | │              │ │            │ │         │  │
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └────┬─────┘  │
│         │                │                │               │        │
│         ▼                ▼                ▼               ▼        │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    二级路由：智能体配置                       │   │
│  │  context 参数（target_language / repair_source / fuzz_method）│   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 一级路由：智能体选择矩阵

| agent_id | 智能体名称 | 工作流模式 | 触发关键词 | 适用场景 |
|----------|-----------|-----------|-----------|---------|
| `retrieval_planning_coding_agent` | 智能开发 | `retrieval_planning_coding` | "生成"、"编写"、"开发"、"创建" | 从需求生成PLC代码 |
| `compilation_debugging_agent` | 智能修复 | `compilation_debugging` | "修复"、"错误"、"编译失败"、"bug" | 修复编译/测试/验证失败 |
| `formal_validation_agent` | 形式化验证 | `formal_validation` | "验证"、"属性"、"安全"、"formal" | 形式化证明代码属性 |
| `fuzz_testing_agent` | 智能测试 | `fuzz_testing` | "测试"、"模糊测试"、"fuzz" | 生成测试用例发现缺陷 |
| `enhanced_super_agent` | 超级智能体 | `enhanced_super_agent` | 通用/复杂任务 | 多智能体协作自动分解 |
| `single_agent_llm` | 单智能体LLM | 无工作流 | 闲聊/简单问答 | 基础对话，无需工作流 |

### 2.3 意图识别决策树

```
用户输入消息
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│  意图识别关键词检测                                           │
│                                                           │
│  是否包含 "生成" / "编写" / "开发" / "创建" 代码类词汇？     │
│      │                                                     │
│      ├── 是 ──► 选择【智能开发】agent_id                    │
│      │         需要进一步判断：target_language (ST/SCL/FBD) │
│      │                                                     │
│      ├── 否 ▼                                              │
│                                                           │
│  是否包含 "修复" / "错误" / "bug" / "编译失败" 修复类词汇？ │
│      │                                                     │
│      ├── 是 ──► 选择【智能修复】agent_id                    │
│      │         需要进一步判断：repair_source                │
│      │                                                     │
│      ├── 否 ▼                                              │
│                                                           │
│  是否包含 "验证" / "属性" / "formal" / "安全证明" 验证类？  │
│      │                                                     │
│      ├── 是 ──► 选择【形式化验证】agent_id                  │
│      │                                                     │
│      ├── 否 ▼                                              │
│                                                           │
│  是否包含 "测试" / "fuzz" / "用例" 测试类词汇？              │
│      │                                                     │
│      ├── 是 ──► 选择【智能测试】agent_id                    │
│      │         需要进一步判断：fuzz_method                  │
│      │                                                     │
│      └── 否 ──► 选择【单智能体LLM】或【超级智能体】          │
└───────────────────────────────────────────────────────────┘
```

### 2.4 二级路由：智能体配置参数详解

#### 2.4.1 智能开发 - context 参数

| 参数 | 类型 | 必填 | 说明 | 可选值 |
|------|------|------|------|--------|
| `target_language` | String | **是** | 目标代码语言 | `ST`、`SCL`、`FBD` |
| `enable_socratic_spec` | Boolean | 否 | 启用苏格拉底需求梳理 | `true`、`false`（默认） |
| `compiler_type` | String | 否 | 编译器类型 | `matiec`（默认）、`rusty` |
| `rpc_pipeline` | Array | 否 | 后续管道 | `['fuzz']`、`['formal']`、`['fuzz','formal']` |
| `template` | String | 否 | 代码模板 | `start_stop`、`timer`、`pid` 等 |

**语言选择决策**：

| 语言 | 标识 | 适用场景 | 特点 |
|------|------|---------|------|
| **ST** | `ST` | 通用PLC编程（IEC 61131-3标准） | 默认选项，兼容性最好 |
| **SCL** | `SCL` | 西门子S7系列PLC | 首行必须为FUNCTION/FUNCTION_BLOCK |
| **FBD** | `FBD` | 可视化编程、复杂逻辑 | 输出PLCopen XML，跳过编译验证 |

#### 2.4.2 智能修复 - context 参数

| 参数 | 类型 | 必填 | 说明 | 可选值 |
|------|------|------|------|--------|
| `repair_source` | String | **是** | 修复来源方向 | 见下方修复方向表 |
| `repair_targets` | Array | 否 | 具体修复目标列表 | 可多选组合 |
| `compiler_type` | String | 否 | 编译器类型 | `matiec`（默认）、`rusty` |
| `repair_failure_notes` | String | 否 | 失败摘要信息 | 用户粘贴的错误信息 |

**修复方向表**：

| repair_source | 修复方向 | 错误来源 | 典型场景 |
|--------------|---------|---------|---------|
| `compile` | 编译错误修复 | matiec/rusty编译器stderr | ST代码语法问题 |
| `test_failure` | 测试失败修复 | FuzzGen/智能测试失败用例 | 模糊测试检出的缺陷 |
| `formal_validation_failure` | 验证失败修复 | PLCverif反例轨迹 | 属性不满足的代码问题 |
| `multi` | 多方向综合修复 | 组合上述多种错误 | 同时处理编译+测试+验证问题 |

#### 2.4.3 形式化验证 - context 参数

| 参数 | 类型 | 必填 | 说明 | 可选值 |
|------|------|------|------|--------|
| `properties` | Array/Object | 否 | 待验证形式化属性 | 若不提供则自动生成 |
| `natural_language_requirements` | String | 否 | 自然语言需求描述 | 系统自动转换为属性 |
| `compiler_type` | String | 否 | 编译器类型 | `matiec`（默认）、`rusty` |

#### 2.4.4 智能测试 - context 参数

| 参数 | 类型 | 必填 | 说明 | 可选值 |
|------|------|------|------|--------|
| `fuzz_method` | String | **是** | 模糊测试方法 | 见下方测试方法表 |
| `case_count` | Integer | 否 | 测试用例数量 | 默认50，建议20-200 |
| `enable_fuzz_test` | Boolean | 否 | 是否启用模糊测试 | 默认由系统配置决定 |

**测试方法选择表**：

| fuzz_method | 名称 | 适用场景 | 推荐用例数 |
|-------------|------|---------|-----------|
| `random` | 随机测试 | 快速发现基础问题 | 50-100 |
| `boundary` | 边界值测试 | 安全关键属性、数值变量 | 20-30 |
| `scenario` | 场景驱动测试 | 常规功能验证 | 30-50 |
| `dse` | 域敏感性测试 | 复杂数学运算 | 40-60 |
| `afl` | 覆盖率导向测试 | 代码深度测试 | 100-200 |
| `llm` | LLM生成测试 | 复杂逻辑验证 | 20-30 |

### 2.5 完整调用示例

#### 示例1：智能开发 - ST语言电机控制

```json
POST /api/chat/stream
{
  "message": "生成一个电机启停控制的ST程序，带有过载保护",
  "agent_id": "retrieval_planning_coding_agent",
  "context": {
    "target_language": "ST",
    "enable_socratic_spec": true,
    "compiler_type": "matiec",
    "rpc_pipeline": ["fuzz", "formal"]
  }
}
```

**响应流程**：
```
苏格拉底需求梳理（可选）
    ↓
检索智能体（RAG知识库）
    ↓
规划智能体（方案设计）
    ↓
编码智能体（生成ST代码）
    ↓
静默编译（matiec验证）
    ↓
智能测试（FuzzGen模糊测试）
    ↓
形式化验证（PLCverif属性检查）
    ↓
输出 stage_guidance 引导卡片
```

#### 示例2：智能开发 - SCL语言西门子专用

```json
POST /api/chat/stream
{
  "message": "用SCL语言编写一个PID温度控制器",
  "agent_id": "retrieval_planning_coding_agent",
  "context": {
    "target_language": "SCL",
    "compiler_type": "matiec"
  }
}
```

**SCL特殊要求**：
- 首行必须为 `FUNCTION` 或 `FUNCTION_BLOCK`
- 支持西门子特有的表达式语法

#### 示例3：智能开发 - FBD功能块图

```json
POST /api/chat/stream
{
  "message": "用FBD画一个启停互锁的控制逻辑",
  "agent_id": "retrieval_planning_coding_agent",
  "context": {
    "target_language": "FBD"
  }
}
```

**FBD特殊流程**：
```
FBD专用子系统（Agents_FBD）
    ├── Analyst（需求分析）
    ├── Designer（XML功能块设计）
    └── Debugger（XSD校验）
        ↓
输出：PLCopen XML（跳过编译验证）
```

#### 示例4：智能修复 - 编译错误修复

```json
POST /api/chat/stream
{
  "message": "修复这段ST代码的编译错误",
  "agent_id": "compilation_debugging_agent",
  "context": {
    "repair_source": "compile",
    "compiler_type": "rusty"
  },
  "uploadedFiles": [
    {"file_id": "file_xxx", "content": "VAR_GLOBAL..."}
  ]
}
```

**三层修复架构**：
```
第一层：快速修复
├── 检测VAR_GLOBAL错误
└── 自动转换为VAR_EXTERNAL + 顶层声明
        ↓ 失败
第二层：规则修复（STComprehensiveValidator）
├── 拼写错误修正
├── 运算符修复（:=、=、=>）
├── 分号补全、括号匹配
└── TIME字面量格式修正（T# → TIME#）
        ↓ 失败
第三层：打补丁修复（LLM Debugging Agent）
├── LLM分析错误并生成补丁
├── 补丁格式：Remove line X / Add after line X
└── 迭代修复（最多6次）
```

#### 示例5：智能修复 - 测试失败修复

```json
POST /api/chat/stream
{
  "message": "修复模糊测试发现的问题",
  "agent_id": "compilation_debugging_agent",
  "context": {
    "repair_source": "test_failure",
    "repair_failure_notes": "测试用例tc_023失败：输出值超出预期范围"
  }
}
```

#### 示例6：形式化验证 - 自动生成属性

```json
POST /api/chat/stream
{
  "message": "验证这段ST代码的安全性属性",
  "agent_id": "formal_validation_agent",
  "context": {
    "natural_language_requirements": "电机停止时抱闸必须闭合",
    "compiler_type": "matiec"
  }
}
```

#### 示例7：形式化验证 - 提供自定义属性

```json
POST /api/chat/stream
{
  "message": "使用以下属性验证代码",
  "agent_id": "formal_validation_agent",
  "context": {
    "properties": [
      {
        "property_description": "电机停止时抱闸必须闭合",
        "property": {
          "job_req": "assertion",
          "pattern_id": "general invariance",
          "params": {"vars": ["motor_running", "brake_engaged"]}
        }
      }
    ]
  }
}
```

#### 示例8：智能测试 - 边界值测试

```json
POST /api/chat/stream
{
  "message": "对这段温度控制代码进行边界值测试",
  "agent_id": "fuzz_testing_agent",
  "context": {
    "fuzz_method": "boundary",
    "case_count": 30
  }
}
```

#### 示例9：超级智能体 - 复杂任务自动分解

```json
POST /api/chat/stream
{
  "message": "我需要实现一个完整的控制系统，包括代码生成、测试和验证",
  "agent_id": "enhanced_super_agent",
  "context": {
    "super_agent_mode": "e2e",
    "target_language": "ST"
  }
}
```

### 2.6 超级智能体意图识别伪代码

```python
def classify_intent(message: str) -> dict:
    """
    意图识别：根据用户消息返回agent_id和context配置
    """
    message_lower = message.lower()
    
    # 1. 代码生成类意图
    if any(kw in message_lower for kw in ["生成", "编写", "开发", "创建", "实现", "write", "generate", "create"]):
        return {
            "agent_id": "retrieval_planning_coding_agent",
            "context": {
                "target_language": detect_language(message),  # ST/SCL/FBD
                "rpc_pipeline": detect_pipeline(message)     # fuzz/formal
            }
        }
    
    # 2. 修复类意图
    if any(kw in message_lower for kw in ["修复", "错误", "bug", "编译失败", "fix", "error", "debug"]):
        return {
            "agent_id": "compilation_debugging_agent",
            "context": {
                "repair_source": detect_repair_source(message)  # compile/test_failure/formal
            }
        }
    
    # 3. 验证类意图
    if any(kw in message_lower for kw in ["验证", "属性", "formal", "安全", "证明", "verify", "validate", "property"]):
        return {
            "agent_id": "formal_validation_agent",
            "context": {}
        }
    
    # 4. 测试类意图
    if any(kw in message_lower for kw in ["测试", "fuzz", "用例", "test", "fuzzing"]):
        return {
            "agent_id": "fuzz_testing_agent",
            "context": {
                "fuzz_method": detect_fuzz_method(message)  # random/boundary/scenario等
            }
        }
    
    # 5. 默认：超级智能体或单智能体
    return {
        "agent_id": "enhanced_super_agent" if is_complex_task(message) else "single_agent_llm",
        "context": {}
    }


def detect_language(message: str) -> str:
    """检测目标编程语言"""
    msg_lower = message.lower()
    if "scl" in msg_lower or "西门子" in message:
        return "SCL"
    if "fbd" in msg_lower or "功能块" in message or "图形" in message:
        return "FBD"
    return "ST"  # 默认ST


def detect_repair_source(message: str) -> str:
    """检测修复来源"""
    msg_lower = message.lower()
    if "测试" in message or "用例" in message or "fuzz" in msg_lower:
        return "test_failure"
    if "验证" in message or "formal" in msg_lower:
        return "formal_validation_failure"
    return "compile"  # 默认编译错误


def detect_fuzz_method(message: str) -> str:
    """检测模糊测试方法"""
    msg_lower = message.lower()
    if "边界" in message or "boundary" in msg_lower:
        return "boundary"
    if "场景" in message or "scenario" in msg_lower:
        return "scenario"
    if "覆盖" in message or "coverage" in msg_lower:
        return "afl"
    return "random"  # 默认随机
```

### 2.7 快速参考卡片

#### 智能开发速查

```
┌─────────────────────────────────────────────────────────┐
│  智能开发 - retrieval_planning_coding_agent              │
├─────────────────────────────────────────────────────────┤
│  入口：POST /api/chat/stream                            │
│                                                         │
│  必填参数：                                             │
│  ├── message: 需求描述                                  │
│  └── context.target_language: ST | SCL | FBD           │
│                                                         │
│  可选参数：                                             │
│  ├── enable_socratic_spec: true（启用需求梳理）          │
│  ├── compiler_type: matiec | rusty（编译器）            │
│  └── rpc_pipeline: ['fuzz'] | ['formal']（后续管道）    │
│                                                         │
│  返回事件：st_code_json / compilation_report_json /     │
│           stage_guidance                                │
└─────────────────────────────────────────────────────────┘
```

#### 智能修复速查

```
┌─────────────────────────────────────────────────────────┐
│  智能修复 - compilation_debugging_agent                  │
├─────────────────────────────────────────────────────────┤
│  入口：POST /api/chat/stream                            │
│                                                         │
│  必填参数：                                             │
│  ├── message: 修复需求                                  │
│  └── context.repair_source: compile | test_failure |    │
│                               formal_validation_failure │
│                                                         │
│  可选参数：                                             │
│  ├── repair_targets: ["test_failure", "formal..."]（多选）│
│  ├── compiler_type: matiec | rusty                      │
│  └── repair_failure_notes: 失败摘要                    │
│                                                         │
│  返回事件：compilation_report_json / stage_guidance     │
└─────────────────────────────────────────────────────────┘
```

#### 形式化验证速查

```
┌─────────────────────────────────────────────────────────┐
│  形式化验证 - formal_validation_agent                   │
├─────────────────────────────────────────────────────────┤
│  入口：POST /api/chat/stream                            │
│                                                         │
│  必填参数：                                             │
│  └── message: 验证需求                                  │
│                                                         │
│  可选参数：                                             │
│  ├── properties: 形式化属性JSON                         │
│  ├── natural_language_requirements: 自然语言需求        │
│  └── compiler_type: matiec | rusty                      │
│                                                         │
│  返回事件：formal_report_json / counterexample          │
└─────────────────────────────────────────────────────────┘
```

#### 智能测试速查

```
┌─────────────────────────────────────────────────────────┐
│  智能测试 - fuzz_testing_agent                          │
├─────────────────────────────────────────────────────────┤
│  入口：POST /api/chat/stream                            │
│                                                         │
│  必填参数：                                             │
│  └── message: 测试需求                                  │
│                                                         │
│  可选参数：                                             │
│  ├── fuzz_method: random | boundary | scenario |        │
│  │             dse | afl | llm                        │
│  ├── case_count: 50（默认）                            │
│  └── enable_fuzz_test: true                            │
│                                                         │
│  返回事件：fuzz_report_json / stage_guidance            │
└─────────────────────────────────────────────────────────┘
```

---

## 四、核心开放接口详细设计

下面每个接口的详细设计，都按统一格式描述：

1. 基础信息
2. 请求入参
3. 响应出参
4. 状态码规范
5. 调用示例
6. 说明与约束

### 3.1 通用请求头

| Header          | 是否必填 | 说明                                                   |
| --------------- | -------- | ------------------------------------------------------ |
| `Content-Type`  | 是       | `application/json`；上传类接口按 `multipart/form-data` |
| `Authorization` | 否       | 建议使用 `Bearer <token>`                              |
| `X-Request-Id`  | 否       | 请求追踪ID                                             |
| `X-Session-Id`  | 否       | 会话追踪ID                                             |

### 3.2 通用错误码

| HTTP状态码 | 业务含义              | 建议处理                       |
| ---------- | --------------------- | ------------------------------ |
| `200`      | 成功                  | 正常处理                       |
| `400`      | 参数错误/校验失败     | 修正请求参数                   |
| `401`      | 未认证/Token无效/过期 | 重新登录或刷新凭证             |
| `403`      | 无权限/账号禁用       | 联系管理员或检查授权           |
| `404`      | 资源不存在            | 检查ID、路径或查询条件         |
| `409`      | 资源冲突              | 账号重复、状态冲突时重试或改名 |
| `429`      | 频率受限              | 降低请求频率                   |
| `500`      | 服务内部异常          | 稍后重试或联系运维             |
| `503`      | 服务不可用            | 等待恢复                       |

### 3.3 统一字段约定

1. 请求里 `sessionId` 和 `requestId` 建议优先使用驼峰命名。
2. `st_code`、`temp_token`、`user_id` 这类已在后端代码中使用的字段，文档中保持原样，避免接入方误解。
3. 流式接口返回的 `data:` 是SSE事件体，不是普通JSON响应体。
4. 上传类接口统一使用 `multipart/form-data`，不要用JSON直接传文件。

### 3.4 智能体类型及参数说明

#### 3.4.1 智能体类型

| 标识                   | 名称        | 工作流                 | 能力描述                                 |
| ---------------------- | ----------- | ---------------------- | ---------------------------------------- |
| `enhanced_super_agent` | 超级智能体  | `enhanced_super_agent` | 支持多智能体协作，自动拆解并执行复杂任务 |
| `single_agent_llm`     | 单智能体LLM | 无工作流               | 基础大模型对话能力，不启用复杂工作流     |

以下为完整智能体配置表：

| agent_id                          | 智能体名称  | 工作流模式                  | 说明                             |
| --------------------------------- | ----------- | --------------------------- | -------------------------------- |
| `retrieval_planning_coding_agent` | 智能开发    | `retrieval_planning_coding` | 知识检索 + 方案规划 + 代码生成   |
| `compilation_debugging_agent`     | 智能修复    | `compilation_debugging`     | 编译错误/测试失败/验证失败修复   |
| `formal_validation_agent`         | 形式化验证  | `formal_validation`         | 安全性验证 + 属性检查 + 反例生成 |
| `fuzz_testing_agent`              | 智能测试    | `fuzz_testing`              | 模糊测试用例生成 + 覆盖率报告    |
| `enhanced_super_agent`            | 超级智能体  | `enhanced_super_agent`      | 多智能体协作，自动任务分解       |
| `single_agent_llm`                | 单智能体LLM | 无工作流                    | 基础LLM对话，不使用复杂工作流    |

#### 3.4.2 Chat接口context通用参数

调用 `/api/chat/stream`、`/api/chat` 接口时，通过 `context` 入参控制智能体执行逻辑，参数说明如下：

| context字段            | 类型    | 说明                                                         | 适用智能体         |
| ---------------------- | ------- | ------------------------------------------------------------ | ------------------ |
| `model_id`             | String  | 指定使用的模型ID，值来自 `/api/models` 返回的模型列表中的 `id` | 全部               |
| `temperature`          | Float   | 采样温度，控制随机性。取值范围 0.0~2.0，值越大回答越随机     | 全部               |
| `top_p`               | Float   | 核采样阈值，取值范围 0.0~1.0                                 | 全部               |
| `top_k`               | Integer | 限制每次采样时考虑的最高概率Token数                           | 全部               |
| `max_tokens`          | Integer | 最大生成的Token数量                                           | 全部               |
| `repair_source`        | String  | 修复来源方向：`compile`编译失败、`test_failure`测试失败、`formal_validation_failure`验证失败、`multi`多来源 | 智能修复           |
| `repair_targets`       | Array   | 具体修复目标列表，例：`["test_failure", "formal_validation_failure"]` | 智能修复           |
| `compiler_type`        | String  | 编译器类型：`rusty` / `matiec`                               | 智能修复、智能开发 |
| `target_language`      | String  | 目标代码语言：`ST`、`SCL`、`FBD`                             | 智能开发           |
| `enable_fuzz_test`     | Boolean | 是否启用模糊测试                                             | 超级智能体         |
| `fuzz_method`          | String  | 模糊测试方法：`random`、`boundary`、`scenario`、`dse`、`afl`、`llm`等 | 智能测试           |
| `case_count`           | Integer | 测试用例生成数量，默认50                                     | 智能测试           |
| `enable_socratic_spec` | Boolean | 是否启用苏格拉底式需求梳理                                   | 智能开发           |
| `super_agent_mode`     | String  | 超级智能体运行模式：`e2e`端到端、`routing`智能路由           | 超级智能体         |

> **说明**：
> - `model_id` 参数指定后，系统会使用对应模型的 `chat_model` 配置作为实际调用模型
> - `temperature`、`top_p`、`top_k`、`max_tokens` 为可选参数，不传时使用模型默认配置
> - 可通过 `/api/models/{model_id}/params` 接口获取指定模型的默认参数值

#### 3.4.3 智能修复 - 修复方向配置

通过 `context.repair_source` 指定修复场景：

| repair_source取值           | 修复方向       | 补充说明                         |
| --------------------------- | -------------- | -------------------------------- |
| `compile`                   | 编译错误修复   | 修复ST/SCL代码语法类问题         |
| `test_failure`              | 测试失败修复   | 修复模糊测试检出的缺陷           |
| `formal_validation_failure` | 验证失败修复   | 修复形式化验证反例对应的代码问题 |
| `multi`                     | 多方向综合修复 | 同时处理编译、测试、验证各类问题 |

##### 3.4.3.1 两层修复架构详解

智能修复采用**两层递进式修复策略**（外加一个前置的快速修复层）：

```
┌─────────────────────────────────────────────────────────────┐
│           快速修复（0层）                                    │
│  • 检测 VAR_GLOBAL 错误                                    │
│  • 自动转换为 VAR_EXTERNAL + 顶层声明                      │
│  • 编译验证通过则直接成功                                  │
└─────────────────────────────────────────────────────────────┘
                            ↓ 未通过
┌─────────────────────────────────────────────────────────────┐
│           第一层：规则修复（Rule-based Fix）                  │
│  STComprehensiveValidator / STRepairEngine                   │
│  • 拼写错误修正                                           │
│  • 运算符修复（:=、=、=>等）                              │
│  • 分号补全                                                │
│  • 括号匹配修复                                            │
│  • TIME字面量格式修正（T# → TIME#）                       │
│  • 结构闭合检查（END_IF/END_FOR等）                       │
│  • 编译验证通过则成功，否则进入第二层                      │
└─────────────────────────────────────────────────────────────┘
                            ↓ 未通过
┌─────────────────────────────────────────────────────────────┐
│           第二层：打补丁修复（Patch-based LLM Fix）         │
│  STPatchRepair + Debugging Agent (LLM)                     │
│  • LLM分析错误并生成补丁操作                               │
│  • 补丁格式：Remove line X / Add after line X             │
│  • STPatchRepair解析并应用补丁                            │
│  • 迭代修复直到成功或达到最大次数（最多6次）               │
└─────────────────────────────────────────────────────────────┘
```

**各修复方向详细说明**：

| 修复方向 | 错误来源 | 修复策略 | 特殊处理 |
|----------|----------|----------|----------|
| `compile` | matiec/rusty编译器stderr | 快速修复 → 规则修复 → 打补丁修复 | VAR_GLOBAL自动转换 |
| `test_failure` | FuzzGen/智能测试失败用例 | 根因分析 → 代码修复 | 失败上下文摘要自动带入 |
| `formal_validation_failure` | PLCverif反例轨迹 | 属性归一化 → 反例分析 → 修复 | PropertyFixer预处理 |

##### 3.4.3.2 修复流程时序

```
用户粘贴ST代码 + 选择修复方向
    ↓
编译确认当前状态
    ↓
进入 debugging_compilation 循环（最多3次）
    ↓
┌──────────────────────────────────────┐
│ 0层：快速修复                         │
│ 检测 VAR_GLOBAL → VAR_EXTERNAL        │
│ ↓ 失败                               │
│ 1层：规则修复（最多1次）             │
│ STComprehensiveValidator              │
│ ↓ 失败                               │
│ 2层：打补丁修复（迭代最多6次）      │
│ LLM生成补丁 → STPatchRepair应用      │
└──────────────────────────────────────┘
    ↓
循环直至成功或达到最大次数
    ↓
修复成功 → 保存新ST文件 → 下发stage_guidance
```

##### 3.4.3.3 打补丁修复详解

打补丁修复是第二层核心，使用LLM生成精确的代码修改操作：

**LLM输出格式**：
```
- Remove line 5: "VAR i: INT"
+ Add after line 5: "VAR
    i : INT;"
```

**STPatchRepair组件**：
- 解析补丁操作（Remove/Add）
- 验证行号精确匹配
- 应用补丁到原代码
- 验证修复后代码结构完整

#### 3.4.4 智能开发专属context参数

| context字段            | 类型    | 说明                                                         |
| ---------------------- | ------- | ------------------------------------------------------------ |
| `target_language`      | String  | 目标代码语言：`ST`(结构化文本)、`SCL`(结构化控制语言)、`FBD`(功能块图) |
| `template`             | String  | 代码模板，可选值：`start_stop`、`timer`、`pid`等             |
| `language_hint`        | String  | 语言补充提示，用于提升代码生成准确率                         |
| `enable_socratic_spec` | Boolean | 开启苏格拉底式需求梳理                                       |
| `socratic_skip`        | Boolean | 跳过需求梳理环节，直接生成代码                               |
| `compiler_type`        | String  | 编译器类型：`rusty` / `matiec`（默认matiec）               |
| `rpc_pipeline`         | Array   | 后续管道：`['fuzz']`、`['formal']`、`['fuzz','formal']`     |

##### 3.4.4.1 智能开发完整流程（ST/SCL）

智能开发工作流 `retrieval_planning_coding` 的完整流程如下：

| 阶段 | 智能体/模块 | 职责 | 输出 |
|------|-------------|------|------|
| 0. **需求梳理（可选）** | 苏格拉底引擎 | 通过苏格拉底式提问引导用户完善需求，生成结构化需求规格书 | `socratic_spec_md` |
| 1. **需求理解** | 检索智能体 | 从RAG知识库检索相关PLC问题、算法、示例与实现思路 | 相关案例与参考方案 |
| 2. **需求规划** | 规划智能体 | 需求分析、技术方案、变量设计、实现步骤 | 结构化实现计划 |
| 3. **代码生成** | 编码智能体 | 根据规划结果生成目标语言代码 | `.st` / `.scl` 文件 |
| 4. **静默编译** | STCompilerTool | 调用matiec/rusty编译器验证代码正确性 | 编译结果 |
| 5. **质量保障（可选）** | FuzzGen / PLCverif | 覆盖率驱动测试或形式化属性验证 | 测试报告/验证报告 |

##### 3.4.4.2 三种开发语言对比

| 语言 | 标识 | 代码格式 | 特点 | 适用场景 |
|------|------|----------|------|----------|
| **ST** | `ST` | 结构化文本 `.st` | IEC 61131-3标准，通用性强 | 通用PLC编程 |
| **SCL** | `SCL` | 结构化控制语言 `.scl` | 西门子专用，首行须为`FUNCTION`/`FUNCTION_BLOCK` | 西门子S7系列 |
| **FBD** | `FBD` | PLCopen XML `.xml` | 功能块图，图形化编程 | 复杂逻辑、流程控制 |

**FBD特殊说明**：
- FBD不走通用编码智能体，而是调用**专用FBD子系统**（Analyst → Designer → Debugger）
- 跳过检索、规划、静默编译阶段
- 输出为可直接导入CODESYS等环境的PLCopen XML

##### 3.4.4.3 苏格拉底需求梳理

当 `context.enable_socratic_spec = true` 时，智能开发会先进入**苏格拉底需求梳理阶段**：

**六步引导流程**：

| 步骤 | 标题 | 收集内容 |
|------|------|----------|
| 1 | 设备基本信息 | 资产ID、位置、控制类型、安全等级 |
| 2 | 输入信号 | 输入变量名、类型、描述 |
| 3 | 输出信号 | 输出变量名、类型、描述 |
| 4 | 控制逻辑 | 布尔逻辑规则、运行状态 |
| 5 | 安全与互锁 | 故障安全策略、互锁条件 |
| 6 | 确认需求 | 完整规格书确认 |

**意图类型**：

| 意图 | 触发条件 | 系统行为 |
|------|----------|----------|
| `clarify` | 默认 | 继续提问，引导下一个字段 |
| `auto_fill_module` | 用户要求补全某模块 | 仅补全指定模块 |
| `auto_fill_full` | 用户要求补全全部 | 补全所有缺失字段 |
| `generate_doc` | 核心字段已完整 | 生成最终Markdown规格文档 |

**用户可说的跳过关键词**：
- "跳过需求梳理"、"跳过所有问题"、"直接生成代码"、"直接写代码"
| `compiler_type`        | String  | 编译器类型：`rusty` / `matiec`，用于生成后的编译验证       |

##### 3.4.4.1 智能开发完整流程（ST/SCL/FBD）

智能开发支持三种PLC编程语言，每种语言有独立的处理流水线：

**ST语言（Structured Text）完整流程**：

```
用户需求输入
    ↓
可选：苏格拉底需求梳理（enable_socratic_spec=true）
    ↓
检索智能体（RAG知识库召回相关案例）
    ↓
规划智能体（需求分析 → 技术方案 → 变量设计 → 实现步骤）
    ↓
编码智能体（生成ST代码，IEC 61131-3标准）
    ↓
静默编译（matiec/rusty编译器验证）
    ↓
可选后续管道：
  • rpc_pipeline=['fuzz'] → 智能测试
  • rpc_pipeline=['formal'] → 形式化验证
    ↓
stage_guidance（分步引导卡片）
```

**SCL语言（Structured Control Language）流程**：

```
用户需求输入
    ↓
（同ST的检索、规划流程）
    ↓
编码智能体（生成SCL代码）
    • 首行必须为 FUNCTION 或 FUNCTION_BLOCK
    • 支持西门子特有的表达式语法
    ↓
静默编译
    ↓
后续处理（同ST）
```

**FBD语言（Function Block Diagram）流程**：

```
用户需求输入
    ↓
FBD专用子系统（Agents_FBD）
    • Analyst（需求分析）
    • Designer（XML功能块设计）
    • Debugger（XSD校验与自修复）
    ↓
输出：PLCopen XML格式
    ↓
结束（不经过编译验证）
```

##### 3.4.4.2 ST/SCL/FBD输出格式对比

| 语言 | 标识 | 输出格式 | 文件类型 | 编译验证 | 适用场景 |
|------|------|----------|----------|----------|----------|
| ST | `ST` | 文本代码 | `.st` | 是（matiec/rusty） | IEC 61131-3通用 |
| SCL | `SCL` | 文本代码 | `.st`/`.scl` | 是 | 西门子PLC专用 |
| FBD | `FBD` | XML结构 | PLCopen XML | 否 | 可视化编程 |

##### 3.4.4.3 苏格拉底需求梳理流程

苏格拉底需求梳理是智能开发的**可选前置阶段**，通过六步引导帮助用户完善需求：

| 步骤 | 标题 | 收集内容 | 输出 |
|------|------|----------|------|
| 1 | 设备基本信息 | 资产ID、位置、控制类型、安全等级 | 元数据 |
| 2 | 输入信号 | 输入变量名、类型、描述 | 变量表 |
| 3 | 输出信号 | 输出变量名、类型、描述 | 变量表 |
| 4 | 控制逻辑 | 布尔逻辑规则、运行状态 | 逻辑描述 |
| 5 | 安全与互锁 | 故障安全策略、互锁条件 | 安全约束 |
| 6 | 确认需求 | 完整规格书确认 | 结构化规格书 |

**苏格拉底事件流**：

| 工作流事件 | 说明 |
|-----------|------|
| `workflow_start` | 启动工作流，含 `socratic_phase: True` |
| `token` | 对话文本片段（前端显示） |
| `socratic_spec` | 规格书增量摘要（含进度） |
| `spec_generated` | **关键事件**：规格书已就绪 |
| `socratic_skip` | 用户跳过（说"跳过需求梳理"） |
| `stage_guidance` | 引导进入编码阶段 |

#### 3.4.5 智能测试专属context参数

| context字段   | 类型    | 说明                                                         |
| ------------- | ------- | ------------------------------------------------------------ |
| `fuzz_method` | String  | 测试策略：`random`、`boundary`、`scenario`、`dse`、`afl`、`llm`等 |
| `case_count`  | Integer | 生成测试用例数量，默认值50                                   |
| `enable_fuzz_test` | Boolean | 是否启用模糊测试（默认由系统配置决定）                    |

##### 3.4.5.1 测试方法详解

智能测试（fuzz_testing）支持以下测试方法，外部调用时通过 `context.fuzz_method` 指定：

| 方法标识 | 名称 | 原理 | 适用场景 | 特点 |
|----------|------|------|----------|------|
| `random` | 随机测试 | 随机生成输入值 | 快速发现基础问题 | 执行快，覆盖面广但深度浅 |
| `boundary` | 边界值测试 | 聚焦极值、边界条件 | 安全关键属性验证 | 适合数值类型变量测试 |
| `scenario` | 场景驱动测试 | 基于业务场景生成用例 | 常规功能验证 | 用例质量高，针对性强 |
| `dse` | 域敏感性测试 | 基于域的敏感性分析 | 复杂数学运算验证 | 适合数值计算类代码 |
| `afl` | 覆盖率导向测试 | American Fuzzy Lop算法 | 代码深度测试 | 最大化代码路径覆盖 |
| `llm` | LLM生成测试 | 大语言模型生成用例 | 复杂逻辑验证 | 智能理解代码意图 |

##### 3.4.5.2 智能测试完整流程

```
用户请求测试（或从智能开发自动续跑）
    ↓
提取ST代码（从文件或消息）
    ↓
静默编译验证（确保代码可编译）
    ↓
FuzzGen执行测试
    ↓
覆盖率分析 + 报告生成
    ↓
失败时写入 fuzz_failure_context
    ↓
输出 stage_guidance 引导后续操作
```

##### 3.4.5.3 测试报告数据结构

智能测试完成后会输出 `fuzz_report_json` 流式事件，对应数据结构：

| 字段 | 类型 | 说明 |
|------|------|------|
| `report_id` | String | 报告唯一ID |
| `workflow_success` | Boolean | 测试流程是否正常结束 |
| `total_test_cases` | Integer | 执行总用例数 |
| `passed` | Integer | 测试通过用例数 |
| `failed` | Integer | 测试失败用例数 |
| `coverage_statistics` | Object | 代码覆盖率统计 |
| `rq_metrics` | Object | 可靠性、质量等指标 |
| `case_type_statistics` | Object | 各类用例数量分布 |
| `failed_details` | Array | 失败用例详情 |

##### 3.4.5.4 触发方式

| 触发方式 | 说明 |
|----------|------|
| 独立模式 `fuzz_testing` | 通过 `agent_id=fuzz_testing_agent` 直接进入fuzz_testing工作流 |
| 智能开发续跑 `rpc_pipeline: ['fuzz']` | 编译成功后自动续跑模糊测试 |
| 历史兼容 `enable_fuzz_test: true` | 兼容旧版参数 |

#### 3.4.6 流式接口响应事件类型

流式接口（智能开发/修复/测试/验证）返回不同类型事件，用于前端解析展示流程与数据：

| 事件type                  | 说明           | 应用场景                                 |
| ------------------------- | -------------- | ---------------------------------------- |
| `session_id`              | 会话ID         | 长连接首次建立时返回                     |
| `agent_start`             | 智能体启动     | 智能体开始执行任务                       |
| `workflow_start`          | 工作流启动     | 进入编译、测试、验证等主流程             |
| `phase_start`             | 阶段开始       | 需求分析、代码生成、用例执行等子阶段启动 |
| `phase_complete`          | 阶段完成       | 单个执行阶段结束                         |
| `token`                   | 增量文本流     | AI实时输出的文本内容                     |
| `st_code_json`            | ST结构化代码   | ST代码生成完成，返回JSON格式代码         |
| `fbd_code_json`           | FBD代码        | 功能块图代码生成完成                     |
| `compilation_report_json` | 编译报告       | 代码编译检查完成，返回报告数据           |
| `fuzz_report_json`        | 模糊测试报告   | 智能测试流程结束，输出测试报告           |
| `formal_report_json`      | 形式化验证报告 | 形式化验证完成，输出验证报告             |
| `stage_guidance`          | 操作引导       | 引导用户进行下一步操作                   |
| `spec_generated`          | 规格书生成     | 需求梳理完成，输出规格文档               |
| `download_ready`          | 下载就绪       | 报告/文件可触发下载                      |
| `error`                   | 异常提示       | 任务执行出现错误                         |
| `complete`                | 任务结束       | 全流程执行完毕                           |

### 3.5 聊天会话模块接口详细设计

#### 3.5.1 流式聊天 - `/api/chat/stream`

**基础信息**

| 项目     | 内容               |
| -------- | ------------------ |
| 接口名称 | 流式聊天主入口     |
| 接口地址 | `/api/chat/stream` |
| 请求方式 | POST               |
| 建议对外 | 是                 |
| 认证     | 需要               |

**请求入参**

```json
{
  "message": "用户消息内容",
  "sessionId": "会话ID（可选）",
  "agent_id": "retrieval_planning_coding_agent",
  "context": {
    "target_language": "ST",
    "enable_socratic_spec": true
  }
}
```

| 参数      | 类型   | 必填 | 说明                              |
| --------- | ------ | ---- | --------------------------------- |
| message   | String | 是   | 用户消息内容                      |
| sessionId | String | 否   | 会话ID，不传则自动创建新会话      |
| agent_id  | String | 否   | 智能体ID，默认 `single_agent_llm` |
| context   | Object | 否   | 上下文参数，详见3.4节             |

**响应出参**

流式响应（SSE格式）：

```
data: {"type": "content", "content": "生成的文本内容"}
data: {"type": "thinking", "content": "思考过程"}
data: {"type": "tool_call", "tool": "工具名称", "args": {}}
data: {"type": "tool_result", "result": {}}
data: {"type": "done", "sessionId": "会话ID"}
```

**状态码规范**

| 状态码 | 说明         |
| ------ | ------------ |
| 200    | 成功（流式） |
| 401    | 未授权       |
| 429    | 频率受限     |

**调用示例**

```bash
curl -X POST https://api.example.com/api/chat/stream \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "生成一个ST语言的电机控制程序", "agent_id": "retrieval_planning_coding_agent"}'
```

#### 3.5.2 非流式聊天 - `/api/chat`

**基础信息**

| 项目     | 内容           |
| -------- | -------------- |
| 接口名称 | 非流式聊天接口 |
| 接口地址 | `/api/chat`    |
| 请求方式 | POST           |
| 建议对外 | 是             |
| 认证     | 需要           |
| 数据格式 | JSON           |

**请求入参**

| 参数名          | 数据类型 | 是否必填 | 说明                       |
| --------------- | -------- | -------- | -------------------------- |
| `message`       | String   | 是       | 用户输入内容               |
| `sessionId`     | String   | 否       | 会话唯一标识               |
| `agentId`       | String   | 否       | 指定调用的智能体标识       |
| `userId`        | String   | 否       | 用户唯一标识               |
| `context`       | Object   | 否       | 扩展上下文，控制智能体行为 |
| `uploadedFiles` | Array    | 否       | 已上传文件信息集合         |

**响应出参**

| 参数名            | 数据类型 | 说明                     |
| ----------------- | -------- | ------------------------ |
| `code`            | Integer  | 业务状态码               |
| `message`         | String   | 结果提示信息             |
| `success`         | Boolean  | 接口执行是否成功         |
| `response`        | String   | 智能体返回的最终内容     |
| `agent_id`        | String   | 实际调度的智能体标识     |
| `real_agent_used` | Boolean  | 是否启用内部工作流       |
| `error`           | String   | 异常详情（仅失败时返回） |
| `usage`           | Object   | Token使用统计（可选）    |

**成功响应示例**

```json
{
  "success": true,
  "response": "已根据需求生成代码。",
  "agent_id": "retrieval_planning_coding_agent",
  "real_agent_used": true
}
```

**失败响应示例**

```json
{
  "success": false,
  "response": "抱歉，当前无可用的智能体服务。请检查配置。",
  "agent_id": "enhanced_super_agent",
  "error": "No agent available"
}
```

#### 3.5.3 流式接口返回示例

```text
data: {"type":"session_id","session_id":"sess_xxx"}
data: {"type":"agent_start","agent_name":"retrieval_planning_coding_agent","content":"智能开发"}
data: {"type":"token","content":"正在分析需求..."}
```

#### 3.5.4 获取可用模型列表 - `/api/models`

**基础信息**

| 项目     | 内容               |
| -------- | ------------------ |
| 接口名称 | 获取可用模型列表   |
| 接口地址 | `/api/models`      |
| 请求方式 | GET                |
| 建议对外 | 是                 |
| 认证     | 需要               |

**功能说明**

获取系统中所有可用的AI模型列表，包括模型的标识、名称、分组、是否免费、是否推荐等信息。前端可使用此接口动态渲染模型选择器，支持按模型分组显示。

**请求参数**

> 说明：此接口为GET请求，无请求体参数。可通过URL查询参数进行筛选。

| 参数名   | 类型   | 位置  | 是否必填 | 说明                           |
| -------- | ------ | ----- | -------- | ------------------------------ |
| 无       | -      | -     | -        | 无需参数，直接调用即可获取全量 |

**响应出参**

| 参数名          | 类型    | 说明                     |
| --------------- | ------- | ------------------------ |
| `success`       | Boolean | 接口调用是否成功         |
| `models`        | Array   | 模型列表数组             |
| `default_model` | String  | 系统默认模型ID           |
| `error`         | String  | 错误信息（仅失败时返回） |

**models数组子项字段**

| 参数名       | 类型    | 说明                                                   |
| ------------ | ------- | ------------------------------------------------------ |
| `id`         | String  | 模型唯一标识符（对应 MODEL_REGISTRY 中的key）          |
| `label`      | String  | 模型显示名称，供前端UI展示                             |
| `group`      | String  | 模型所属分组，用于前端按分组显示（如"本地模型"、"OpenAI"） |
| `is_free`    | Boolean | 是否为免费模型                                         |
| `recommended`| Boolean | 是否为推荐模型                                         |
| `description`| String  | 模型描述信息（如有）                                   |

**响应示例**

成功响应：

```json
{
  "success": true,
  "models": [
    {
      "id": "qwen3:4b",
      "label": "Qwen3-4B",
      "group": "本地模型",
      "is_free": true,
      "recommended": true,
      "description": ""
    },
    {
      "id": "deepseek-chat",
      "label": "deepseek-chat",
      "group": "深度求索",
      "is_free": false,
      "recommended": true,
      "description": ""
    },
    {
      "id": "gpt-5.4",
      "label": "gpt-5.4",
      "group": "OpenAI",
      "is_free": false,
      "recommended": true,
      "description": "模型不可用（yibuapi渠道无此模型）"
    },
    {
      "id": "Agents4PLC",
      "label": "Agents4PLC",
      "group": "Agents4PLC",
      "is_free": true,
      "recommended": true,
      "description": ""
    }
  ],
  "default_model": "deepseek-chat"
}
```

失败响应：

```json
{
  "success": false,
  "error": "Failed to load models",
  "models": [],
  "default_model": "qwen3:4b"
}
```

**模型分组说明**

系统中的模型按以下分组进行分类：

| 分组名称   | 说明                                           |
| ---------- | ---------------------------------------------- |
| `本地模型` | 本地部署的Ollama模型，无需API费用               |
| `Agents4PLC` | 专用PLC助手模型                               |
| `OpenAI`   | OpenAI系列模型                                 |
| `深度求索` | DeepSeek系列模型                               |
| `智谱AI`   | 智谱GLM系列模型                                |
| `月之暗面` | Kimi系列模型                                   |
| `通义千问` | 阿里Qwen系列模型                               |
| `豆包`     | 字节豆包系列模型                               |
| `Google`   | Google Gemini系列模型                          |

**调用示例**

cURL调用：

```bash
curl -X GET http://localhost:8000/api/models \
  -H "Authorization: Bearer YOUR_TOKEN"
```

JavaScript调用：

```javascript
const response = await fetch('/api/models', {
  method: 'GET',
  headers: {
    'Authorization': 'Bearer YOUR_TOKEN'
  }
});
const data = await response.json();
console.log(data.models);
```

**前端使用建议**

1. **模型分组显示**：使用 `group` 字段对模型进行分组，构建两级选择器（分组选择 → 模型选择）
2. **推荐模型优先**：优先展示 `recommended: true` 的模型
3. **免费模型标识**：可使用 `is_free` 字段为免费模型添加特殊标识
4. **默认值设置**：使用 `default_model` 字段作为新建会话的默认选中项

#### 3.5.5 获取指定模型参数 - `/api/models/{model_id}/params`

**基础信息**

| 项目     | 内容                   |
| -------- | ---------------------- |
| 接口名称 | 获取指定模型默认参数   |
| 接口地址 | `/api/models/{model_id}/params` |
| 请求方式 | GET                    |
| 建议对外 | 是                     |
| 认证     | 需要                   |

**功能说明**

获取指定模型的默认参数配置，供前端初始化模型参数调节面板（如Temperature、Top-P、Top-K、Max Tokens等）。

**请求参数**

| 参数名     | 类型   | 位置 | 是否必填 | 说明         |
| ---------- | ------ | ---- | -------- | ------------ |
| `model_id` | String | Path | 是       | 模型唯一标识 |

**响应出参**

| 参数名       | 类型    | 说明                   |
| ------------ | ------- | ---------------------- |
| `model_id`   | String  | 模型ID                 |
| `temperature`| Float   | 默认温度值（0.0-2.0）  |
| `top_p`      | Float   | 默认Top-P值（0.0-1.0） |
| `top_k`      | Integer | 默认Top-K值            |
| `max_tokens` | Integer | 默认最大Token数        |
| `error`      | String  | 错误信息（仅失败时）   |

**响应示例**

```json
{
  "model_id": "deepseek-chat",
  "temperature": 0.7,
  "top_p": 0.9,
  "top_k": 40,
  "max_tokens": 8000
}
```

**调用示例**

```bash
curl -X GET http://localhost:8000/api/models/deepseek-chat/params \
  -H "Authorization: Bearer YOUR_TOKEN"
```



### 3.6 知识库问答模块接口详细设计

#### 3.6.1 知识库问答 - `/api/knowledge/qa`

**基础信息**

| 项目     | 内容                |
| -------- | ------------------- |
| 接口名称 | 知识库问答          |
| 接口地址 | `/api/knowledge/qa` |
| 请求方式 | POST                |
| 建议对外 | 是                  |
| 认证     | 需要                |

**请求入参**

| 参数名      | 数据类型 | 是否必填 | 说明                       |
| ----------- | -------- | -------- | -------------------------- |
| `query`     | String   | 是       | 用户提问文本               |
| `sessionId` | String   | 否       | 会话唯一标识，用于多轮对话 |
| `topK`      | Integer  | 否       | 知识库检索文档数量         |
| `kbId`      | String   | 否       | 指定目标知识库ID           |
| `language`  | String   | 否       | 回复语言，支持中文/英文    |

**响应出参**

| 参数名       | 数据类型 | 说明                 |
| ------------ | -------- | -------------------- |
| `success`    | Boolean  | 接口调用是否成功     |
| `answer`     | String   | 知识库问答结果       |
| `references` | Array    | 引用的知识库来源列表 |
| `session_id` | String   | 当前会话ID           |

**references子项字段**

| 参数名   | 数据类型 | 说明     |
| -------- | -------- | -------- |
| `title`  | String   | 文档标题 |
| `source` | String   | 来源标识 |

**响应示例**

```json
{
  "success": true,
  "answer": "根据知识库内容，建议采用启动/停止自锁逻辑。",
  "references": [
    {
      "title": "启停控制案例",
      "source": "knowledge_base"
    }
  ],
  "session_id": "kbqa_001"
}
```

#### 3.6.2 知识库流式问答 - `/api/knowledge/chat/stream`

**基础信息**

| 项目     | 内容                         |
| -------- | ---------------------------- |
| 接口名称 | 知识库流式问答               |
| 接口地址 | `/api/knowledge/chat/stream` |
| 请求方式 | POST                         |
| 建议对外 | 是                           |
| 认证     | 需要                         |

**请求入参**

| 参数名             | 数据类型 | 是否必填 | 说明                        |
| ------------------ | -------- | -------- | --------------------------- |
| `question`         | String   | 是       | 用户问题                    |
| `sessionId`        | String   | 否       | 会话ID（可选）              |
| `knowledgeBaseIds` | Array    | 否       | 指定知识库ID列表            |
| `enableWebSearch`  | Boolean  | 否       | 是否启用网络搜索，默认false |
| `topK`             | Integer  | 否       | 知识库检索文档数量          |
| `language`         | String   | 否       | 回复语言                    |

**流式响应字段**

复用通用流式事件规范，核心字段如下：

| 参数名       | 类型   | 说明                                                         |
| ------------ | ------ | ------------------------------------------------------------ |
| `type`       | String | 事件类型：`retrieval`/`token`/`references`/`error`/`complete`等 |
| `content`    | String | 增量输出文本内容                                             |
| `session_id` | String | 会话ID                                                       |

流式响应示例（SSE格式）：

```
data: {"type": "retrieval", "chunks": [{"content": "检索到的知识片段", "source": "来源"}]}
data: {"type": "token", "content": "生成的回答内容"}
data: {"type": "references", "sources": [{"title": "参考来源", "url": "链接"}]}
data: {"type": "complete", "sessionId": "会话ID"}
```

**补充说明**

1. 适合大篇幅回答场景，实时推送生成内容；
2. 基于 `sessionId` 支持多轮连续对话；
3. 可同步输出知识库引用来源信息。

#### 3.6.3 清空问答会话 - `/api/knowledge/qa/sessions/{session_id}/clear`

**基础信息**

| 项目     | 内容                                            |
| -------- | ----------------------------------------------- |
| 接口地址 | `/api/knowledge/qa/sessions/{session_id}/clear` |
| 请求方式 | POST                                            |
| 建议对外 | 是                                              |
| 认证     | 需要                                            |

**请求入参**

| 参数       | 类型   | 位置 | 说明   |
| ---------- | ------ | ---- | ------ |
| session_id | String | Path | 会话ID |

**响应出参**

```json
{
  "code": 200,
  "message": "Session cleared successfully"
}
```

#### 3.6.4 增强型知识问答 - `/api/knowledge/chat/enhanced`

**基础信息**

| 项目     | 内容                           |
| -------- | ------------------------------ |
| 接口地址 | `/api/knowledge/chat/enhanced` |
| 请求方式 | POST                           |
| 建议对外 | 是                             |
| 认证     | 需要                           |

**请求入参**

```json
{
  "question": "用户问题",
  "sessionId": "会话ID（可选）",
  "context": {
    "enableReasoning": true,
    "maxTokens": 2000,
    "temperature": 0.7
  }
}
```

**响应出参**

```json
{
  "code": 200,
  "data": {
    "answer": "增强型回答",
    "reasoning": "推理过程",
    "sources": [],
    "sessionId": "会话ID"
  }
}
```

#### 3.6.5 获取知识聊天会话列表 - `/api/knowledge/chat/sessions`

**基础信息**

| 项目     | 内容                           |
| -------- | ------------------------------ |
| 接口地址 | `/api/knowledge/chat/sessions` |
| 请求方式 | GET                            |
| 建议对外 | 是                             |
| 认证     | 需要                           |

**请求入参**

| 参数     | 类型    | 位置  | 说明             |
| -------- | ------- | ----- | ---------------- |
| page     | Integer | Query | 页码，默认1      |
| pageSize | Integer | Query | 每页条数，默认20 |

**响应出参**

```json
{
  "code": 200,
  "data": {
    "sessions": [
      {
        "sessionId": "会话ID",
        "title": "会话标题",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
        "messageCount": 10
      }
    ],
    "total": 100,
    "page": 1,
    "pageSize": 20
  }
}
```

#### 3.6.6 获取知识聊天会话详情 - `/api/knowledge/chat/sessions/{session_id}`

**基础信息**

| 项目     | 内容                                        |
| -------- | ------------------------------------------- |
| 接口地址 | `/api/knowledge/chat/sessions/{session_id}` |
| 请求方式 | GET                                         |
| 建议对外 | 是                                          |
| 认证     | 需要                                        |

**响应出参**

```json
{
  "code": 200,
  "data": {
    "sessionId": "会话ID",
    "title": "会话标题",
    "createdAt": "2024-01-01T00:00:00Z",
    "updatedAt": "2024-01-01T00:00:00Z",
    "knowledgeBaseIds": ["kb_id_1"],
    "messageCount": 10
  }
}
```

#### 3.6.7 获取知识聊天会话消息 - `/api/knowledge/chat/sessions/{session_id}/messages`

**基础信息**

| 项目     | 内容                                                 |
| -------- | ---------------------------------------------------- |
| 接口地址 | `/api/knowledge/chat/sessions/{session_id}/messages` |
| 请求方式 | GET                                                  |
| 建议对外 | 是                                                   |
| 认证     | 需要                                                 |

**响应出参**

```json
{
  "code": 200,
  "data": {
    "messages": [
      {
        "role": "user",
        "content": "用户问题",
        "timestamp": "2024-01-01T00:00:00Z"
      },
      {
        "role": "assistant",
        "content": "回答内容",
        "references": [],
        "timestamp": "2024-01-01T00:00:01Z"
      }
    ]
  }
}
```

#### 3.6.8 创建知识聊天会话 - `/api/knowledge/chat/sessions`

**基础信息**

| 项目     | 内容                           |
| -------- | ------------------------------ |
| 接口地址 | `/api/knowledge/chat/sessions` |
| 请求方式 | POST                           |
| 建议对外 | 是                             |
| 认证     | 需要                           |

**请求入参**

```json
{
  "title": "会话标题（可选）",
  "knowledgeBaseIds": ["kb_id_1", "kb_id_2"]
}
```

**响应出参**

```json
{
  "code": 200,
  "data": {
    "sessionId": "新会话ID",
    "title": "会话标题",
    "createdAt": "2024-01-01T00:00:00Z"
  }
}
```

#### 3.6.9 删除知识聊天会话 - `/api/knowledge/chat/sessions/{session_id}`

**基础信息**

| 项目     | 内容                                        |
| -------- | ------------------------------------------- |
| 接口地址 | `/api/knowledge/chat/sessions/{session_id}` |
| 请求方式 | DELETE                                      |
| 建议对外 | 是                                          |
| 认证     | 需要                                        |

**响应出参**

```json
{
  "code": 200,
  "message": "Session deleted successfully"
}
```

#### 3.6.10 清空知识聊天会话 - `/api/knowledge/chat/sessions/{session_id}/clear`

**基础信息**

| 项目     | 内容                                              |
| -------- | ------------------------------------------------- |
| 接口地址 | `/api/knowledge/chat/sessions/{session_id}/clear` |
| 请求方式 | POST                                              |
| 建议对外 | 是                                                |
| 认证     | 需要                                              |

**响应出参**

```json
{
  "code": 200,
  "message": "Session cleared successfully"
}
```

#### 3.6.11 重命名知识聊天会话 - `/api/knowledge/chat/sessions/{session_id}/rename`

**基础信息**

| 项目     | 内容                                               |
| -------- | -------------------------------------------------- |
| 接口地址 | `/api/knowledge/chat/sessions/{session_id}/rename` |
| 请求方式 | PUT                                                |
| 建议对外 | 是                                                 |
| 认证     | 需要                                               |

**请求入参**

```json
{
  "title": "新会话标题"
}
```

**响应出参**

```json
{
  "code": 200,
  "message": "Session renamed successfully",
  "data": {
    "sessionId": "会话ID",
    "title": "新会话标题"
  }
}
```

#### 3.6.12 智能知识问答 - `/api/knowledge/chat/intelligent`

**基础信息**

| 项目     | 内容                              |
| -------- | --------------------------------- |
| 接口地址 | `/api/knowledge/chat/intelligent` |
| 请求方式 | POST                              |
| 建议对外 | 是                                |
| 认证     | 需要                              |

**请求入参**

```json
{
  "question": "用户问题",
  "sessionId": "会话ID（可选）",
  "mode": "auto",
  "options": {
    "enableMultiHop": true,
    "maxRetrievalDepth": 3
  }
}
```

**响应出参**

```json
{
  "code": 200,
  "data": {
    "answer": "智能问答回答",
    "reasoningChain": ["推理步骤1", "推理步骤2"],
    "sources": [],
    "confidence": 0.95,
    "sessionId": "会话ID"
  }
}
```

### 3.7 知识库管理接口

提供知识库条目增删改查、检索、分类、数据统计等能力，支撑RAG检索相关配置与维护。

#### 3.7.1 `/api/knowledge/items`

**接口说明**：新增单条知识库条目
**请求方式**：`POST`

**请求入参**

| 参数名    | 类型   | 是否必填 | 说明             |
| --------- | ------ | -------- | ---------------- |
| title     | String | 是       | 条目标题         |
| content   | String | 是       | 条目正文内容     |
| category  | String | 否       | 所属分类         |
| tags      | Array  | 否       | 标签数组         |
| brand     | String | 否       | 关联品牌         |
| file_path | String | 否       | 关联附件文件路径 |

**响应出参**

| 参数名  | 类型    | 说明                         |
| ------- | ------- | ---------------------------- |
| success | Boolean | 接口调用是否成功             |
| item    | Object  | 新建完成的知识库条目完整信息 |

#### 3.7.2 `/api/knowledge/items/{knowledge_id}`

**接口说明**：根据知识库ID执行查询、更新、删除操作

**路径参数**

| 参数名       | 类型   | 是否必填 | 说明             |
| ------------ | ------ | -------- | ---------------- |
| knowledge_id | String | 是       | 知识库条目唯一ID |

**GET（查询条目详情）**

请求方式：`GET`

响应出参：

| 参数名  | 类型    | 说明             |
| ------- | ------- | ---------------- |
| success | Boolean | 接口调用是否成功 |
| item    | Object  | 知识库条目详情   |

**PUT（更新条目）**

请求方式：`PUT`

请求入参：

| 参数名   | 类型   | 是否必填 | 说明         |
| -------- | ------ | -------- | ------------ |
| title    | String | 否       | 条目标题     |
| content  | String | 否       | 条目正文内容 |
| category | String | 否       | 所属分类     |
| tags     | Array  | 否       | 标签数组     |
| brand    | String | 否       | 关联品牌     |

**DELETE（删除条目）**

请求方式：`DELETE`

响应出参：

| 参数名  | 类型    | 说明             |
| ------- | ------- | ---------------- |
| success | Boolean | 接口调用是否成功 |
| message | String  | 操作结果描述     |

#### 3.7.3 `/api/knowledge/search`

**接口说明**：关键词检索知识库，支持筛选、分页
**请求方式**：`POST / GET`

**请求参数**

| 参数名    | 类型    | 是否必填 | 说明         |
| --------- | ------- | -------- | ------------ |
| q         | String  | 是       | 搜索关键词   |
| category  | String  | 否       | 按分类筛选   |
| brand     | String  | 否       | 按品牌筛选   |
| page      | Integer | 否       | 页码，默认1  |
| page_size | Integer | 否       | 每页数据条数 |

**响应出参**

| 参数名  | 类型    | 说明             |
| ------- | ------- | ---------------- |
| success | Boolean | 接口调用是否成功 |
| items   | Array   | 检索结果条目列表 |
| total   | Integer | 数据总条数       |

#### 3.7.4 知识库统计接口

**接口1**：`/api/knowledge/statistics`
**接口2**：`/api/knowledge/stats`

**接口说明**：获取知识库整体统计数据（条目总数、分类分布等）
**请求方式**：`GET`

**响应出参**

| 参数名             | 类型    | 说明             |
| ------------------ | ------- | ---------------- |
| success            | Boolean | 接口调用是否成功 |
| statistics / stats | Object  | 统计指标集合     |

#### 3.7.5 `/api/knowledge/categories`

**接口说明**：获取知识库全部分类列表，用于分类管理与数据筛选
**请求方式**：`GET`

**响应出参**

| 参数名       | 类型    | 说明             |
| ------------ | ------- | ---------------- |
| `success`    | Boolean | 接口调用是否成功 |
| `categories` | Array   | 分类名称列表     |

#### 3.7.6 `/api/knowledge/rag-sync-state`

**接口说明**：查询RAG向量库同步状态、向量总量及同步进度
**请求方式**：`GET`

**响应出参**

| 参数名         | 类型    | 说明            |
| -------------- | ------- | --------------- |
| `enabled`      | Boolean | RAG功能是否启用 |
| `message`      | String  | 状态描述信息    |
| `state`        | Object  | 详细同步状态    |
| `vector_count` | Integer | 向量库数据总量  |

#### 3.7.7 `/api/knowledge/sync-rag`

**接口说明**：触发知识库向RAG向量库同步数据，支持全量重建
**请求方式**：`POST`

**请求入参**

| 参数名          | 类型    | 是否必填 | 说明                   |
| --------------- | ------- | -------- | ---------------------- |
| `force_rebuild` | Boolean | 否       | 是否强制全量重建向量库 |

**响应出参**

| 参数名    | 类型    | 说明             |
| --------- | ------- | ---------------- |
| `success` | Boolean | 接口调用是否成功 |
| `task_id` | String  | 同步任务唯一ID   |

#### 3.7.8 `/api/knowledge/rag-config`

**接口说明**：查询/更新RAG检索全局配置（检索数量、相似度阈值）

- 查询配置：`GET`
- 更新配置：`POST`

**GET响应出参**

| 参数名    | 类型    | 说明             |
| --------- | ------- | ---------------- |
| `success` | Boolean | 接口调用是否成功 |
| `config`  | Object  | RAG配置项集合    |

**POST请求入参**

| 参数名      | 类型    | 是否必填 | 说明               |
| ----------- | ------- | -------- | ------------------ |
| `top_k`     | Integer | 否       | 单次检索返回条目数 |
| `threshold` | Number  | 否       | 相似度置信度阈值   |

#### 3.7.9 `/api/knowledge/search-rag`

**接口说明**：基于向量相似度做语义检索，返回关联知识条目
**请求方式**：`POST / GET`

**请求参数**

| 参数名  | 类型    | 是否必填 | 说明         |
| ------- | ------- | -------- | ------------ |
| `q`     | String  | 是       | 检索文本     |
| `top_k` | Integer | 否       | 结果返回条数 |

#### 3.7.10 `/api/knowledge/qa/sessions`

**接口说明**：获取/创建知识库问答会话，支撑多轮对话

- 查询会话列表：`GET`
- 创建新会话：`POST`

**POST请求入参**

| 参数名  | 类型   | 是否必填 | 说明           |
| ------- | ------ | -------- | -------------- |
| `title` | String | 否       | 会话标题       |
| `kbId`  | String | 否       | 绑定的知识库ID |

**GET响应出参**

| 参数名     | 类型    | 说明             |
| ---------- | ------- | ---------------- |
| `success`  | Boolean | 接口调用是否成功 |
| `sessions` | Array   | 问答会话列表     |

#### 3.7.11 `/api/knowledge/chat/sessions/{session_id}`

**接口说明**：会话管理接口，支持**查询详情、清空历史、删除会话、重命名**

**路径参数**

| 参数名       | 类型   | 是否必填 | 说明       |
| ------------ | ------ | -------- | ---------- |
| `session_id` | String | 是       | 会话唯一ID |

**响应出参**

| 参数名    | 类型    | 说明                     |
| --------- | ------- | ------------------------ |
| `success` | Boolean | 接口调用是否成功         |
| `message` | String  | 操作结果描述             |
| `session` | Object  | 会话详情（仅查询时返回） |

### 3.8 形式化验证相关接口

#### 3.8.1 `/api/formal-validation/validate`

**接口说明**：对ST代码执行语法及形式化属性预校验，判断是否可进入正式验证流程
**请求方式**：`POST`

**请求入参**

| 参数名                          | 数据类型     | 是否必填 | 说明             |
| ------------------------------- | ------------ | -------- | ---------------- |
| `st_code`                       | String       | 是       | 待校验ST代码     |
| `properties`                    | Array/Object | 否       | 待验证形式化属性 |
| `natural_language_requirements` | String       | 否       | 自然语言需求描述 |

**响应出参**

| 参数名       | 数据类型 | 说明                 |
| ------------ | -------- | -------------------- |
| `success`    | Boolean  | 接口调用是否成功     |
| `validation` | Object   | 完整校验结果         |
| `is_ready`   | Boolean  | 是否满足正式验证条件 |

**validation子结构**

| 参数名        | 类型    | 说明               |
| ------------- | ------- | ------------------ |
| `is_valid`    | Boolean | 代码及属性是否合法 |
| `errors`      | Array   | 错误信息列表       |
| `warnings`    | Array   | 警告信息列表       |
| `suggestions` | Array   | 优化建议列表       |

**响应示例**

```json
{
  "success": true,
  "validation": {
    "is_valid": true,
    "errors": [],
    "warnings": [],
    "suggestions": []
  },
  "is_ready": true
}
```

#### 3.8.2 形式化验证报告结构

流式事件 `formal_report_json` 对应数据结构：

| 字段                   | 类型    | 说明                       |
| ---------------------- | ------- | -------------------------- |
| `report_id`            | String  | 报告唯一标识ID             |
| `workflow_success`     | Boolean | 验证流程是否正常完成       |
| `properties`           | Array   | 待验证属性集合             |
| `property_results`     | Array   | 单条属性验证结果列表       |
| `passed`               | Integer | 验证通过的属性数量         |
| `failed`               | Integer | 验证失败的属性数量         |
| `not_checked`          | Integer | 未执行检查的属性数量       |
| `counterexample`       | Object  | 反例数据（验证失败时返回） |
| `verification_time_ms` | Integer | 验证耗时，单位：毫秒       |
| `model_info`           | Object  | 代码模型相关信息           |

**属性结构说明**

| 字段             | 类型   | 说明                                                         |
| ---------------- | ------ | ------------------------------------------------------------ |
| `type`           | String | 属性类型：`safety`安全属性、`liveness`活性属性、`invariant`不变量 |
| `description`    | String | 属性文字描述                                                 |
| `expr`           | String | 形式化逻辑表达式                                             |
| `status`         | String | 验证结果：`passed`/`failed`/`not_checked`/`error`            |
| `counterexample` | Object | 反例详情（仅失败时返回）                                     |

#### 3.8.3 `/api/formal-validation/convert-natural-language`

**接口说明**：将自然语言需求转换为标准形式化验证属性
**请求方式**：`POST`

**请求入参**

| 参数名                          | 数据类型 | 是否必填 | 说明                 |
| ------------------------------- | -------- | -------- | -------------------- |
| `natural_language_requirements` | String   | 是       | 原始自然语言需求描述 |
| `language`                      | String   | 否       | 目标代码语言：`ST`   |
| `formalLanguage`                | String   | 否       | 形式化语言：`LTL`    |

**响应出参**

| 参数名             | 数据类型 | 说明                     |
| ------------------ | -------- | ------------------------ |
| `success`          | Boolean  | 接口调用是否成功         |
| `properties`       | Array    | 转换完成的形式化属性列表 |
| `count`            | Integer  | 生成属性总数量           |
| `formalSpec`       | String   | 转换后的形式化规约       |
| `confidence`       | Number   | 转换置信度               |
| `alternativeSpecs` | Array    | 备选规约列表             |

#### 3.8.4 `/api/formal-validation/format-examples`

**接口说明**：获取ST代码、形式化属性编写示例与规范
**请求方式**：`GET`

**响应出参**

| 参数名     | 数据类型 | 说明             |
| ---------- | -------- | ---------------- |
| `success`  | Boolean  | 接口调用是否成功 |
| `examples` | Object   | 各类格式示例集合 |

**examples结构**

```json
{
  "examples": [
    {
      "language": "LTL",
      "description": "永远为真",
      "syntax": "G (condition)"
    },
    {
      "language": "ST",
      "description": "最终为真",
      "syntax": "F (condition)"
    }
  ]
}
```

#### 3.8.5 `/api/compilation/validate`

**接口说明**：检查ST代码语法、编译错误，并提供自动修复能力
**请求方式**：`POST`

**请求入参**

| 参数名         | 数据类型 | 是否必填 | 说明                                        |
| -------------- | -------- | -------- | ------------------------------------------- |
| `st_code`      | String   | 是       | 待检测的ST源代码                            |
| `compilerType` | String   | 否       | 编译器类型：`rusty`或`matiec`，默认`matiec` |
| `language`     | String   | 否       | 代码语言：`ST`                              |

**响应出参**

| 参数名          | 数据类型 | 说明                   |
| --------------- | -------- | ---------------------- |
| `success`       | Boolean  | 接口调用是否成功       |
| `validation`    | Object   | 编译校验结果           |
| `is_ready`      | Boolean  | 是否可进入自动修复流程 |
| `fixed_code`    | String   | 修复后的完整代码       |
| `fixes_applied` | Array    | 已执行的修复项列表     |

**编译报告结构**

流式事件 `compilation_report_json` 对应数据结构：

| 字段               | 类型    | 说明                           |
| ------------------ | ------- | ------------------------------ |
| `workflow_success` | Boolean | 编译流程是否成功               |
| `compiler_type`    | String  | 编译器类型：`rusty` / `matiec` |
| `error_count`      | Integer | 语法错误总数                   |
| `warning_count`    | Integer | 编译警告总数                   |
| `errors`           | Array   | 错误详情列表                   |
| `warnings`         | Array   | 警告详情列表                   |
| `fixed_code`       | String  | 修复后代码                     |
| `fixes_applied`    | Array   | 已应用修复记录                 |
| `suggestions`      | Array   | 优化与修复建议                 |

**错误/警告子项结构**

| 字段       | 类型    | 说明                              |
| ---------- | ------- | --------------------------------- |
| `line`     | Integer | 代码行号                          |
| `column`   | Integer | 代码列号                          |
| `message`  | String  | 错误/警告描述                     |
| `severity` | String  | 级别：`error`错误 / `warning`警告 |
| `code`     | String  | 错误编码                          |

#### 3.8.6 获取形式化报告接口

| 接口地址                                     | 请求方式 | 说明                       |
| -------------------------------------------- | -------- | -------------------------- |
| `/api/formal/reports/{report_id}.json`       | GET      | 获取形式化报告JSON格式     |
| `/api/formal/reports/{report_id}.md`         | GET      | 获取形式化报告Markdown格式 |
| `/api/formal/reports/{report_id}.html`       | GET      | 获取形式化报告HTML格式     |
| `/api/formal/reports/{report_id}/bundle.zip` | GET      | 获取报告压缩包             |

**响应说明**：
- JSON接口返回 `application/json` 格式的报告数据
- Markdown接口返回 `text/markdown` 格式的报告内容
- HTML接口返回 `text/html` 格式的报告页面
- 压缩包接口返回 `application/zip` 格式，包含报告及相关附件

#### 3.8.7 形式化验证完整流程

形式化验证工作流 `formal_validation` 的完整流程如下：

```
用户请求形式化验证
    ↓
提取ST代码（从文件或消息）
    ↓
属性生成（Property Agent）
    或使用用户提供的属性
    ↓
属性预处理
  • 布尔等式改写（rewrite_pattern_bool_equalities）
  • 属性格式修复（PropertyFixer）
  ↓
PLCverif执行验证
    ↓
报告生成（HTML/Markdown）
    ↓
验证失败时输出反例（Counterexample）
    ↓
输出 stage_guidance 引导后续操作
```

#### 3.8.8 属性生成详解

当用户未提供形式化验证属性时，系统会调用**属性生成智能体（Property Agent）**自动生成。

**属性结构**：

```json
[
    {
        "property_description": "中文描述 / English description",
        "property": {
            "job_req": "assertion",  // 或 "pattern"
            "pattern_id": "...",
            "params": {...}
        }
    }
]
```

**属性类型说明**：

| 属性类型 | 标识 | 说明 | 示例 |
|----------|------|------|------|
| 安全属性 | `safety` | 系统永远不会进入危险状态 | "电机停止时抱闸必须闭合" |
| 活性属性 | `liveness` | 系统最终会进入期望状态 | "启动命令发出后电机必然启动" |
| 不变量 | `invariant` | 特定条件下始终保持的条件 | "温度不超过100°C" |

**属性格式示例（LTL）**：

| 描述 | LTL语法 |
|------|---------|
| 永远为真 | `G (condition)` |
| 最终为真 | `F (condition)` |
| 直到某条件为真 | `U` |
| 下一个状态 | `X (condition)` |
| 释放操作符 | `R` |

#### 3.8.9 反例分析与修复

当形式化验证失败时，系统会输出**反例（Counterexample）**：

**反例结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `step` | Integer | 反例发生的步骤数 |
| `variables` | Object | 反例发生时的变量状态 |
| `violated_property` | String | 违反的属性描述 |
| `trace` | Array | 完整的变量轨迹 |

**反例分析器（CounterexampleAnalyzer）**功能：
- 解析PLCverif输出的反例信息
- 追踪反例发生时的变量状态
- 生成针对性的代码修改建议

**修复衔接**：反例信息会自动传递给智能修复（formal_validation_failure方向），进行针对性修复。

### 3.9 模糊测试（Fuzz）相关接口

#### 3.9.1 `/api/fuzz/methods`

**接口说明**：查询系统支持的模糊测试策略列表
**请求方式**：`GET`

**响应出参**

| 参数名    | 数据类型 | 说明             |
| --------- | -------- | ---------------- |
| `success` | Boolean  | 接口调用是否成功 |
| `methods` | Array    | 测试方法列表     |

**methods子项结构**

| 字段          | 类型   | 说明               |
| ------------- | ------ | ------------------ |
| `id`          | String | 方法唯一标识       |
| `name`        | String | 方法中文名称       |
| `description` | String | 功能描述           |
| `strength`    | String | 测试强度：高/中/低 |
| `speed`       | String | 执行速度：快/中/慢 |

**测试方法对照表**

| 标识 | 名称 | 原理 | 适用场景 | 特点 |
|------|------|------|----------|------|
| `random` | 随机测试 | 随机生成输入值 | 快速发现基础问题 | 执行快，覆盖面广但深度浅 |
| `boundary` | 边界值测试 | 聚焦极值、边界条件 | 安全关键属性验证 | 适合数值类型变量测试 |
| `scenario` | 场景驱动测试 | 基于业务场景生成用例 | 常规功能验证 | 用例质量高，针对性强 |
| `dse` | 域敏感性测试 | 基于域的敏感性分析 | 复杂数学运算验证 | 适合数值计算类代码 |
| `afl` | 覆盖率导向测试 | American Fuzzy Lop算法 | 代码深度测试 | 最大化代码路径覆盖 |
| `llm` | LLM生成测试 | 大语言模型生成用例 | 复杂逻辑验证 | 智能理解代码意图 |
| `coverage` | 覆盖率导向 | 最大化代码路径覆盖 | 代码深度测试 | 与AFL配合使用 |
| `property_based` | 属性驱动测试 | 依据形式化属性生成用例 | 安全属性专项验证 | 结合形式化验证使用 |

**测试方法详细说明**：

| 方法 | 适用变量类型 | 推荐用例数量 | 执行速度 |
|------|-------------|-------------|----------|
| `random` | 任意 | 50-100 | 最快 |
| `boundary` | INT, REAL, DINT | 20-30 | 快 |
| `scenario` | 任意 | 30-50 | 中等 |
| `dse` | REAL, LREAL | 40-60 | 中等 |
| `afl` | 任意 | 100-200 | 较慢 |
| `llm` | 任意 | 20-30 | 取决于模型 |

#### 3.9.2 `/api/fuzz/preflight`

**接口说明**：Fuzz测试环境预检，校验依赖服务与运行环境
**请求方式**：`GET`

**响应出参**

| 参数名      | 数据类型 | 说明             |
| ----------- | -------- | ---------------- |
| `success`   | Boolean  | 接口调用是否成功 |
| `preflight` | Object   | 预检整体信息     |

**preflight对象结构**

| 字段       | 类型    | 说明                     |
| ---------- | ------- | ------------------------ |
| `ready`    | Boolean | 环境是否就绪，可开始测试 |
| `checks`   | Array   | 分项检查结果             |
| `warnings` | Array   | 环境警告信息             |
| `errors`   | Array   | 环境异常信息             |

**响应示例**

```json
{
  "success": true,
  "preflight": {
    "ready": true,
    "checks": [
      {"name": "compiler", "status": "ok", "message": "Rusty编译器就绪"},
      {"name": "llm", "status": "ok", "message": "LLM服务正常"},
      {"name": "workspace", "status": "ok", "message": "工作目录可写"}
    ],
    "warnings": [],
    "errors": []
  }
}
```

#### 3.9.3 `/api/fuzz/generate`

**接口说明**：基于ST代码批量生成模糊测试用例
**请求方式**：`POST`

**请求入参**

| 参数名       | 数据类型 | 是否必填 | 说明                   |
| ------------ | -------- | -------- | ---------------------- |
| `st_code`    | String   | 是       | 待测试ST源代码         |
| `method`     | String   | 否       | 测试方法，默认`random` |
| `case_count` | Integer  | 否       | 生成用例数量，默认50   |

**method可选值**：`random`、`boundary`、`scenario`、`coverage`

**响应出参**

| 参数名                | 数据类型 | 说明                       |
| --------------------- | -------- | -------------------------- |
| `success`             | Boolean  | 接口调用是否成功           |
| `method`              | String   | 实际使用的测试方法         |
| `case_count`          | Integer  | 实际生成用例总数           |
| `cases`               | Array    | 测试用例列表               |
| `generation_time_sec` | Number   | 用例生成耗时（秒）         |
| `error`               | String   | 错误描述（调用失败时返回） |
| `metadata`            | Object   | 附加元数据                 |

**cases子项结构**

| 字段               | 类型   | 说明           |
| ------------------ | ------ | -------------- |
| `id`               | String | 用例唯一ID     |
| `name`             | String | 用例名称       |
| `type`             | String | 用例类型       |
| `inputs`           | Object | 输入变量键值对 |
| `expected_outputs` | Object | 预期输出结果   |
| `description`      | String | 用例说明       |

**响应示例**

```json
{
  "success": true,
  "method": "boundary",
  "case_count": 10,
  "cases": [
    {
      "id": "tc_001",
      "name": "边界值测试_最大值",
      "type": "boundary",
      "inputs": {"x": 100, "y": 0},
      "expected_outputs": {"z": 100},
      "description": "测试x为最大值100时的输出"
    }
  ],
  "generation_time_sec": 1.23,
  "metadata": {
    "coverage_hints": ["branch_x_gt_50", "branch_y_eq_0"]
  }
}
```

#### 3.9.4 `/api/fuzz/run`

**接口说明**：执行完整模糊测试流程，输出测试结果
**请求方式**：`POST`

**请求入参**

| 参数名    | 数据类型 | 是否必填 | 说明                                                  |
| --------- | -------- | -------- | ----------------------------------------------------- |
| `message` | String   | 是       | 任务描述/ST代码内容                                   |
| `context` | String   | 否       | JSON格式上下文，支持传入`fuzz_method`、`case_count`等 |

**Fuzz报告结构**

流式事件 `fuzz_report_json` 对应数据结构：

| 字段                   | 类型    | 说明                 |
| ---------------------- | ------- | -------------------- |
| `report_id`            | String  | 报告唯一ID           |
| `workflow_success`     | Boolean | 测试流程是否正常结束 |
| `summary`              | String  | 测试结果摘要         |
| `total_test_cases`     | Integer | 执行总用例数         |
| `passed`               | Integer | 测试通过用例数       |
| `failed`               | Integer | 测试失败用例数       |
| `coverage_statistics`  | Object  | 代码覆盖率统计       |
| `rq_metrics`           | Object  | 可靠性、质量等指标   |
| `case_type_statistics` | Object  | 各类用例数量分布     |
| `failed_details`       | Array   | 失败用例详情         |
| `test_cases`           | Array   | 全量测试用例         |
| `generation_time_sec`  | Number  | 用例生成耗时         |

**响应出参**

| 参数名    | 数据类型 | 说明             |
| --------- | -------- | ---------------- |
| `success` | Boolean  | 接口调用是否成功 |
| `result`  | Object   | 测试执行结果     |

#### 3.9.5 Fuzz测试报告接口

| 接口地址                                       | 请求方式 | 说明                     |
| ---------------------------------------------- | -------- | ------------------------ |
| `/api/fuzz/reports/{report_id}.md`             | GET      | 下载Markdown格式Fuzz报告 |
| `/api/fuzz/reports/{report_id}.md`             | GET      | 下载Markdown格式Fuzz报告 |
| `/api/fuzz/reports/{report_id}/bundle.zip`     | GET      | 下载Fuzz报告压缩包       |
| `/api/fuzz/reports/{report_id}/testcases.json` | GET      | 获取Fuzz测试用例明细     |

**testcases.json响应出参**

| 参数名      | 数据类型 | 说明             |
| ----------- | -------- | ---------------- |
| `success`   | Boolean  | 接口调用是否成功 |
| `testcases` | Array    | 测试用例列表     |
| `total`     | Integer  | 用例总条数       |
| `summary`   | Object   | 测试结果汇总     |

**响应示例**

```json
{
  "code": 200,
  "data": {
    "testCases": [
      {
        "id": "tc_001",
        "input": "测试输入",
        "expected": "预期输出",
        "actual": "实际输出",
        "status": "passed|failed|error",
        "errorMessage": "错误信息（如有）"
      }
    ],
    "summary": {
      "total": 100,
      "passed": 85,
      "failed": 10,
      "error": 5,
      "coverage": 0.78
    }
  }
}
```

### 3.10 认证与账号模块接口详细设计

#### 3.10.1 `/api/auth/login`

**接口说明**：用户登录，返回JWT令牌，用于后续接口身份认证
**请求方式**：`POST`

**请求入参**

| 参数名              | 数据类型 | 是否必填 | 说明                        |
| ------------------- | -------- | -------- | --------------------------- |
| `username_or_email` | String   | 否       | 用户名/邮箱（任选其一登录） |
| `username`          | String   | 否       | 用户名                      |
| `userAccount`       | String   | 否       | 登录账号                    |
| `email`             | String   | 否       | 注册邮箱                    |
| `password`          | String   | 是       | 登录密码                    |

> 说明：账号类字段至少传一项，配合`password`完成登录。

**响应出参**

| 参数名          | 数据类型 | 说明              |
| --------------- | -------- | ----------------- |
| `success`       | Boolean  | 接口是否调用成功  |
| `user`          | Object   | 登录用户基础信息  |
| `token`         | String   | JWT身份令牌       |
| `session.token` | String   | 会话令牌          |
| `expiresIn`     | Integer  | Token有效期（秒） |

**响应示例**

```json
{
  "success": true,
  "user": {
    "id": "1",
    "username": "demo"
  },
  "token": "jwt_token",
  "session": {
    "token": "jwt_token"
  }
}
```

#### 3.10.2 `/api/auth/register`

**接口说明**：新用户账号注册
**请求方式**：`POST`

**请求入参**

| 参数名              | 数据类型 | 是否必填 | 说明      |
| ------------------- | -------- | -------- | --------- |
| `username`          | String   | 否       | 用户名    |
| `userAccount`       | String   | 否       | 登录账号  |
| `username_or_email` | String   | 否       | 账号/邮箱 |
| `password`          | String   | 是       | 登录密码  |
| `email`             | String   | 否       | 绑定邮箱  |
| `userName`          | String   | 否       | 用户昵称  |
| `name`              | String   | 否       | 姓名      |

**响应出参**

| 参数名     | 数据类型 | 说明           |
| ---------- | -------- | -------------- |
| `success`  | Boolean  | 注册结果       |
| `username` | String   | 注册成功的账号 |
| `userId`   | String   | 用户ID         |
| `message`  | String   | 提示信息       |

#### 3.10.3 `/api/auth/validate`

**接口说明**：校验JWT令牌有效性，解析关联用户信息
**请求方式**：`GET / POST`

**请求参数**

| 参数名  | 数据类型 | 是否必填 | 说明                                               |
| ------- | -------- | -------- | -------------------------------------------------- |
| `token` | String   | 否       | 待校验JWT Token（也可从请求头`Authorization`读取） |

**响应出参**

| 参数名      | 数据类型 | 说明             |
| ----------- | -------- | ---------------- |
| `success`   | Boolean  | 令牌是否有效     |
| `user`      | Object   | 令牌所属用户信息 |
| `valid`     | Boolean  | Token是否有效    |
| `userId`    | String   | 用户ID           |
| `expiresAt` | String   | 过期时间         |

#### 3.10.4 `/api/auth/check`

**接口说明**：检查当前客户端登录状态
**请求方式**：`GET`

**响应出参**

| 参数名          | 数据类型 | 说明                         |
| --------------- | -------- | ---------------------------- |
| `success`       | Boolean  | 接口调用状态                 |
| `authenticated` | Boolean  | 当前是否已登录认证           |
| `user`          | Object   | 登录用户信息（未登录则为空） |
| `message`       | String   | 状态描述                     |

#### 3.10.5 `/api/auth/logout`

**接口说明**：用户登出，销毁会话并使令牌失效
**请求方式**：`POST`

**响应出参**

| 参数名       | 数据类型 | 说明         |
| ------------ | -------- | ------------ |
| `success`    | Boolean  | 接口调用结果 |
| `message`    | String   | 操作提示     |
| `logged_out` | Boolean  | 是否完成登出 |

#### 3.10.6 `/api/auth/change-password`

**接口说明**：已登录用户修改密码，需校验旧密码
**请求方式**：`POST`

**请求入参**

| 参数名            | 数据类型 | 是否必填 | 说明       |
| ----------------- | -------- | -------- | ---------- |
| `old_password`    | String   | 是       | 原密码     |
| `new_password`    | String   | 是       | 新密码     |
| `confirmPassword` | String   | 否       | 确认新密码 |

**响应出参**

| 参数名    | 数据类型 | 说明     |
| --------- | -------- | -------- |
| `success` | Boolean  | 修改结果 |
| `message` | String   | 结果描述 |

#### 3.10.7 `/api/auth/forgot-password`

**接口说明**：忘记密码，向邮箱发送重置验证码
**请求方式**：`POST`

**请求入参**

| 参数名  | 数据类型 | 是否必填 | 说明     |
| ------- | -------- | -------- | -------- |
| `email` | String   | 是       | 注册邮箱 |

**响应出参**

| 参数名      | 数据类型 | 说明                                     |
| ----------- | -------- | ---------------------------------------- |
| `success`   | Boolean  | 发送结果                                 |
| `message`   | String   | 提示信息                                 |
| `demo_code` | String   | 调试模式下返回演示验证码，正式环境不返回 |

#### 3.10.8 `/api/auth/verify-code`

**接口说明**：验证邮箱验证码，通过后下发密码重置临时令牌
**请求方式**：`POST`

**请求入参**

| 参数名  | 数据类型 | 是否必填 | 说明             |
| ------- | -------- | -------- | ---------------- |
| `email` | String   | 是       | 接收验证码的邮箱 |
| `code`  | String   | 是       | 邮箱验证码       |

**响应出参**

| 参数名       | 数据类型 | 说明             |
| ------------ | -------- | ---------------- |
| `success`    | Boolean  | 校验结果         |
| `message`    | String   | 提示信息         |
| `temp_token` | String   | 密码重置临时令牌 |
| `valid`      | Boolean  | 验证码是否有效   |

#### 3.10.9 `/api/auth/reset-password`

**接口说明**：使用临时令牌完成密码重置
**请求方式**：`POST`

**请求入参**

| 参数名            | 数据类型 | 是否必填 | 说明               |
| ----------------- | -------- | -------- | ------------------ |
| `temp_token`      | String   | 是       | 验证通过的临时令牌 |
| `new_password`    | String   | 是       | 新登录密码         |
| `confirmPassword` | String   | 否       | 确认新密码         |

**响应出参**

| 参数名    | 数据类型 | 说明     |
| --------- | -------- | -------- |
| `success` | Boolean  | 重置结果 |
| `message` | String   | 结果描述 |

#### 3.10.10 `/api/account/info`

**接口说明**：获取/更新账号信息

- 查询信息：`GET`
- 更新信息：`PUT`

**GET响应出参**

| 参数名      | 数据类型 | 说明     |
| ----------- | -------- | -------- |
| `code`      | Integer  | 状态码   |
| `data`      | Object   | 账号信息 |
| `userId`    | String   | 用户ID   |
| `username`  | String   | 用户名   |
| `email`     | String   | 邮箱     |
| `avatar`    | String   | 头像URL  |
| `role`      | String   | 角色     |
| `createdAt` | String   | 创建时间 |
| `quota`     | Object   | 配额信息 |

**PUT请求入参**

| 参数名     | 数据类型 | 是否必填 | 说明             |
| ---------- | -------- | -------- | ---------------- |
| `username` | String   | 否       | 新用户名         |
| `avatar`   | String   | 否       | 新头像URL        |
| `email`    | String   | 否       | 新邮箱（需验证） |

#### 3.10.11 `/api/user/info`

**接口说明**：获取/编辑当前登录用户个人信息

- 查询信息：`GET`
- 更新信息：`PUT`

**GET响应出参**

| 参数名    | 数据类型 | 说明         |
| --------- | -------- | ------------ |
| `success` | Boolean  | 接口调用结果 |
| `user`    | Object   | 完整用户信息 |
| `user_id` | String   | 用户唯一ID   |

**PUT请求入参**

| 参数名       | 数据类型 | 是否必填 | 说明        |
| ------------ | -------- | -------- | ----------- |
| `userName`   | String   | 否       | 昵称/用户名 |
| `email`      | String   | 否       | 绑定邮箱    |
| `userAvatar` | String   | 否       | 头像地址    |
| `phone`      | String   | 否       | 联系手机号  |
| `company`    | String   | 否       | 所属公司    |

**PUT响应出参**

| 参数名    | 数据类型 | 说明     |
| --------- | -------- | -------- |
| `success` | Boolean  | 更新结果 |
| `message` | String   | 操作提示 |

### 3.11 文件模块接口详细设计

#### 3.11.1 `/api/upload`

**接口说明**：单文件上传，支持自动解析文本并录入知识库
**请求方式**：`POST`（`multipart/form-data`）

**请求入参**

| 参数名           | 数据类型 | 是否必填 | 说明                                  |
| ---------------- | -------- | -------- | ------------------------------------- |
| `file`           | File     | 是       | 待上传文件                            |
| `knowledge_type` | String   | 否       | 知识分类类型                          |
| `tags`           | Array    | 否       | 文件标签                              |
| `purpose`        | String   | 否       | 用途：`chat`、`knowledge`、`avatar`等 |

**响应出参**

| 参数名     | 数据类型 | 说明         |
| ---------- | -------- | ------------ |
| `success`  | Boolean  | 接口调用结果 |
| `file_id`  | String   | 文件唯一标识 |
| `url`      | String   | 文件访问地址 |
| `fileName` | String   | 原文件名     |
| `fileSize` | Integer  | 文件大小     |
| `mimeType` | String   | 文件类型     |

#### 3.11.2 `/api/upload/multiple`

**接口说明**：批量多文件上传
**请求方式**：`POST`（`multipart/form-data`）

**请求入参**

| 参数名           | 数据类型      | 是否必填 | 说明                     |
| ---------------- | ------------- | -------- | ------------------------ |
| `files`          | Array\<File\> | 是       | 批量文件集合（最多10个） |
| `knowledge_type` | String        | 否       | 知识分类类型             |
| `purpose`        | String        | 否       | 用途                     |

**响应出参**

| 参数名    | 数据类型 | 说明                 |
| --------- | -------- | -------------------- |
| `success` | Boolean  | 接口调用结果         |
| `files`   | Array    | 单个文件上传结果列表 |
| `failed`  | Array    | 上传失败的文件列表   |

#### 3.11.3 `/api/files/{file_id}/download`

**接口说明**：文件下载接口，以附件形式返回
**请求方式**：`GET`

- 正常响应：返回文件流，响应头携带`Content-Disposition: attachment`
- 文件不存在：返回404

#### 3.11.4 `/api/files/{file_id}/view`

**接口说明**：文件在线预览，浏览器直接渲染内容
**请求方式**：`GET`

- 正常响应：返回可预览文件内容（文本类文件）或预览页面（非文本类）
- 文件不存在：返回404

### 3.12 智能开发模块接口详细设计

#### 3.12.1 `/api/smart_dev/generate`

**接口说明**：根据自然语言需求自动生成PLC代码，支持ST/SCL/FBD
**请求方式**：`POST`

**请求入参**

| 参数名        | 数据类型 | 是否必填 | 说明                                |
| ------------- | -------- | -------- | ----------------------------------- |
| `requirement` | String   | 是       | 业务需求描述                        |
| `prompt`      | String   | 是       | 生成需求描述（与requirement二选一） |
| `language`    | String   | 是       | 目标语言：`ST`/`SCL`/`FBD`          |
| `template`    | String   | 否       | 代码模板标识                        |
| `options`     | Object   | 否       | 高级生成配置参数                    |
| `sessionId`   | String   | 否       | 会话ID（可选）                      |

**options子参数**

| 参数名            | 类型    | 说明             |
| ----------------- | ------- | ---------------- |
| `includeComments` | Boolean | 是否包含注释     |
| `includeTests`    | Boolean | 是否包含测试用例 |
| `maxLength`       | Integer | 最大生成长度     |
| `_note`           | String  | 补充说明         |
| `_suggestions`    | Array   | 优化建议列表     |
| `keep_files`      | Boolean | 是否保留生成文件 |

**响应出参**

| 参数名            | 数据类型 | 说明                                |
| ----------------- | -------- | ----------------------------------- |
| `success`         | Boolean  | 生成结果                            |
| `content`         | String   | 代码正文                            |
| `code`            | String   | 生成的代码内容（与content同义）     |
| `format`          | String   | 内容格式：`xml`(FBD)/`text`(ST/SCL) |
| `target_language` | String   | 目标编程语言                        |
| `pending`         | Boolean  | 是否需继续补全处理                  |
| `prompt`          | String   | 补全提示词（pending=true时返回）    |
| `note`            | String   | 补充说明                            |
| `suggestions`     | Array    | 优化建议                            |
| `explanation`     | String   | 代码说明                            |
| `sessionId`       | String   | 会话ID                              |
| `tokensUsed`      | Integer  | 使用的Token数量                     |

**响应示例（ST/SCL）**

```json
{
  "success": true,
  "content": "PROGRAM Example ... END_PROGRAM",
  "format": "text",
  "target_language": "ST"
}
```

**FBD格式响应示例**

```json
{
  "success": true,
  "content": "<fc:FunctionBlock xmlns:fc=\"http://www.plcopen.org/xml/tc6_2_1/fbd/\">...",
  "format": "xml",
  "target_language": "FBD"
}
```

#### 3.12.2 `/api/smart_dev/switch_language`

**接口说明**：PLC代码跨语言转换
**请求方式**：`POST`

**请求入参**

| 参数名              | 数据类型 | 是否必填 | 说明                           |
| ------------------- | -------- | -------- | ------------------------------ |
| `requirement`       | String   | 是       | 转换需求描述                   |
| `original_language` | String   | 是       | 原代码语言：`ST`/`SCL`/`FBD`   |
| `target_language`   | String   | 是       | 目标转换语言：`ST`/`SCL`/`FBD` |
| `sessionId`         | String   | 否       | 会话ID（可选）                 |

**响应出参**

| 参数名            | 数据类型 | 说明                   |
| ----------------- | -------- | ---------------------- |
| `success`         | Boolean  | 转换结果               |
| `code`            | String   | 转换后代码             |
| `format`          | String   | 内容格式：`xml`/`text` |
| `target_language` | String   | 目标语言               |
| `pending`         | Boolean  | 是否需继续补全         |
| `prompt`          | String   | 补全提示词             |

**支持转换组合**

| 原语言 | 目标语言 | 说明                                    |
| ------ | -------- | --------------------------------------- |
| ST     | SCL      | 结构化文本 → 西门子控制语言             |
| ST     | FBD      | 结构化文本 → 功能块图                   |
| SCL    | ST       | 西门子控制语言 → 结构化文本             |
| SCL    | FBD      | 西门子控制语言 → 功能块图               |
| FBD    | ST       | 功能块图 → 结构化文本（仅导出逻辑）     |
| FBD    | SCL      | 功能块图 → 西门子控制语言（仅导出逻辑） |

#### 3.12.3 `/api/smart_dev/languages`

**接口说明**：查询系统支持的PLC编程语言列表
**请求方式**：`GET`

**响应出参**

| 参数名      | 数据类型 | 说明         |
| ----------- | -------- | ------------ |
| `success`   | Boolean  | 接口调用结果 |
| `languages` | Array    | 语言信息列表 |
| `default`   | String   | 默认语言     |

**languages子项结构**

| 字段          | 类型   | 说明               |
| ------------- | ------ | ------------------ |
| `code`        | String | 语言标识           |
| `id`          | String | 语言标识（同code） |
| `name`        | String | 语言名称           |
| `description` | String | 功能描述           |
| `icon`        | String | 图标标识           |
| `version`     | String | 标准版本           |
| `features`    | Array  | 支持特性列表       |

**响应示例**

```json
{
  "success": true,
  "languages": [
    {"id": "ST", "code": "ST", "name": "Structured Text (ST)", "description": "结构化文本，IEC 61131-3标准", "icon": "📝", "version": "IEC 61131-3", "features": ["code_gen", "validation", "testing"]},
    {"id": "SCL", "code": "SCL", "name": "Structured Control Language", "description": "西门子PLC专用控制语言", "icon": "⚡", "version": "IEC 61131-3", "features": ["code_gen", "validation"]},
    {"id": "FBD", "code": "FBD", "name": "Function Block Diagram", "description": "功能块图，PLCopen标准XML", "icon": "🔲", "version": "PLCopen TC6", "features": ["code_gen"]}
  ],
  "default": "ST"
}
```

#### 3.12.4 `/api/smart_dev/templates`

**接口说明**：获取代码生成预设模板列表
**请求方式**：`GET`

**请求参数**

| 参数名   | 类型   | 位置  | 说明           |
| -------- | ------ | ----- | -------------- |
| language | String | Query | 过滤语言，可选 |

**响应出参**

| 参数名      | 数据类型 | 说明         |
| ----------- | -------- | ------------ |
| `success`   | Boolean  | 接口调用结果 |
| `templates` | Array    | 模板列表     |

**templates子项结构**

| 字段          | 类型   | 说明                 |
| ------------- | ------ | -------------------- |
| `id`          | String | 模板唯一标识         |
| `name`        | String | 模板名称             |
| `description` | String | 模板描述             |
| `category`    | String | 所属分类（可选）     |
| `language`    | String | 适用语言             |
| `code`        | String | 模板代码内容（可选） |

**响应示例**

```json
{
  "success": true,
  "templates": [
    {"id": "start_stop", "name": "启停控制", "description": "电机启停、按钮控制", "language": "ST"},
    {"id": "timer", "name": "定时器", "description": "延时、定时控制", "language": "ST"},
    {"id": "counter", "name": "计数器", "description": "计数、累计控制", "language": "ST"},
    {"id": "pid", "name": "PID控制", "description": "PID闭环控制", "language": "ST"},
    {"id": "hmi", "name": "HMI交互", "description": "触摸屏交互控制", "language": "SCL"},
    {"id": "alarm", "name": "报警处理", "description": "报警联锁逻辑", "language": "ST"},
    {"id": "interlock", "name": "联锁控制", "description": "设备联锁保护", "language": "FBD"}
  ]
}
```

### 3.13 会话管理接口详细设计

#### 3.13.1 `/api/session/create`

**接口说明**：创建新会话，绑定用户与智能体
**请求方式**：`POST`

**请求入参**

| 参数名                | 数据类型 | 是否必填 | 说明                                 |
| --------------------- | -------- | -------- | ------------------------------------ |
| `user_id`             | String   | 否       | 用户ID，默认`default_user`           |
| `agent_id`            | String   | 否       | 智能体ID，默认`enhanced_super_agent` |
| `context`             | Object   | 否       | 扩展上下文参数                       |
| `metadata`            | Object   | 否       | 会话元数据（标题等）                 |
| `existing_session_id` | String   | 否       | 复用已有会话ID                       |

**响应出参**

| 参数名       | 数据类型 | 说明         |
| ------------ | -------- | ------------ |
| `success`    | Boolean  | 创建结果     |
| `session_id` | String   | 会话唯一ID   |
| `user_id`    | String   | 关联用户ID   |
| `agent_id`   | String   | 关联智能体ID |
| `created_at` | Number   | Unix时间戳   |
| `message`    | String   | 操作提示     |

**响应示例**

```json
{
  "success": true,
  "session_id": "sess_abc123",
  "user_id": "user_001",
  "agent_id": "compilation_debugging_agent",
  "created_at": 1718179200.123,
  "message": "会话创建成功"
}
```

#### 3.13.2 `/api/session/list`

**接口说明**：查询用户会话列表，支持筛选
**请求方式**：`GET`

**请求参数**

| 参数名              | 数据类型 | 是否必填 | 说明                            |
| ------------------- | -------- | -------- | ------------------------------- |
| `user_id`           | String   | 是       | 用户ID                          |
| `include_completed` | Boolean  | 否       | 是否包含已完成会话，默认`false` |
| `agent_id`          | String   | 否       | 按智能体ID筛选                  |

**响应出参**

| 参数名     | 数据类型 | 说明         |
| ---------- | -------- | ------------ |
| `success`  | Boolean  | 接口调用结果 |
| `sessions` | Array    | 会话列表     |
| `total`    | Integer  | 会话总数     |

**sessions子项结构**

| 字段            | 类型    | 说明                                  |
| --------------- | ------- | ------------------------------------- |
| `session_id`    | String  | 会话ID                                |
| `agent_id`      | String  | 智能体ID                              |
| `status`        | String  | 状态：`running`/`completed`/`aborted` |
| `created_at`    | Number  | 创建时间戳                            |
| `updated_at`    | Number  | 最后更新时间戳                        |
| `message_count` | Integer | 消息总数                              |
| `last_message`  | String  | 最后消息摘要                          |
| `metadata`      | Object  | 会话元数据                            |

#### 3.13.3 `/api/session/stats`

**接口说明**：获取会话整体统计数据
**请求方式**：`GET`

**响应出参**

| 参数名    | 数据类型 | 说明         |
| --------- | -------- | ------------ |
| `success` | Boolean  | 接口调用结果 |
| `stats`   | Object   | 统计数据     |

**stats结构**

| 字段                 | 类型    | 说明             |
| -------------------- | ------- | ---------------- |
| `total_sessions`     | Integer | 总会话数         |
| `active_sessions`    | Integer | 活跃会话数       |
| `completed_sessions` | Integer | 已完成会话数     |
| `by_agent`           | Object  | 各智能体会话分布 |

#### 3.13.4 `/api/session/{session_id}`

**接口说明**：获取会话详情、关闭并清理会话

- 获取详情：`GET`
- 删除会话：`DELETE`

**请求参数**

| 参数名     | 类型   | 位置  | 说明                |
| ---------- | ------ | ----- | ------------------- |
| session_id | String | Path  | 会话ID              |
| user_id    | String | Query | 用户ID（GET时可选） |

### 3.14 健康检查模块接口详细设计

#### 3.14.1 `/health`

**接口说明**：简易服务探活，用于负载均衡/监控巡检
**请求方式**：`GET`
**认证**：不需要

**响应出参**

| 参数名      | 数据类型 | 说明                 |
| ----------- | -------- | -------------------- |
| `status`    | String   | 服务状态，`ok`为正常 |
| `service`   | String   | 服务名称             |
| `version`   | String   | API版本              |
| `timestamp` | String   | 时间戳               |

**响应示例**

```json
{
  "status": "ok",
  "service": "Agents4PLC Enhanced API",
  "version": "2.0.0",
  "timestamp": "2024-01-01T00:00:00Z"
}
```

#### 3.14.2 `/api/health`

**接口说明**：全维度健康检查，包含依赖组件状态
**请求方式**：`GET`
**认证**：不需要

**响应出参**

| 参数名         | 数据类型 | 说明                         |
| -------------- | -------- | ---------------------------- |
| `status`       | String   | 整体服务状态                 |
| `service`      | String   | 服务名称                     |
| `version`      | String   | API版本                      |
| `timestamp`    | String   | 时间戳                       |
| `components`   | Object   | 依赖组件状态                 |
| `dependencies` | Object   | 依赖组件状态（同components） |

**dependencies/components结构**

| 字段             | 类型   | 说明              |
| ---------------- | ------ | ----------------- |
| `llm`            | Object | LLM服务状态       |
| `database`       | Object | 数据库状态        |
| `knowledge_base` | Object | 知识库状态        |
| `vector_store`   | Object | 向量库状态        |
| `redis`          | Object | Redis状态（可选） |

**响应示例**

```json
{
  "status": "healthy",
  "service": "Agents4PLC Enhanced API",
  "version": "2.0.0",
  "timestamp": "2024-01-01T00:00:00Z",
  "components": {
    "database": "healthy",
    "redis": "healthy",
    "llm_service": "healthy",
    "knowledge_base": "degraded"
  },
  "dependencies": {
    "llm": {"status": "ok", "message": "可用"},
    "database": {"status": "ok", "message": "连接正常"},
    "knowledge_base": {"status": "ok", "message": "知识库就绪"},
    "vector_store": {"status": "ok", "message": "向量库可用"}
  }
}
```

### 3.15 系统案例模块接口详细设计

#### 3.15.1 `/api/cases`

**接口说明**：获取编程案例库列表，支持分类筛选
**请求方式**：`GET`

**请求参数**

| 参数名     | 数据类型 | 是否必填 | 说明         |
| ---------- | -------- | -------- | ------------ |
| `category` | String   | 否       | 案例分类筛选 |

**响应出参**

| 参数名    | 数据类型 | 说明         |
| --------- | -------- | ------------ |
| `success` | Boolean  | 接口调用结果 |
| `cases`   | Array    | 案例列表     |
| `count`   | Integer  | 案例总数     |

**cases子项结构**

| 字段       | 类型   | 说明       |
| ---------- | ------ | ---------- |
| `id`       | String | 案例ID     |
| `path`     | String | 案例路径   |
| `metadata` | Object | 案例元数据 |

**metadata结构**

| 字段          | 类型   | 说明     |
| ------------- | ------ | -------- |
| `name`        | String | 案例名称 |
| `category`    | String | 所属分类 |
| `description` | String | 案例描述 |
| `difficulty`  | String | 难度等级 |
| `tags`        | Array  | 标签列表 |

### 3.16 多知识库管理接口详细设计

#### 3.16.1 `/api/knowledge/multi-kb/list`

**接口说明**：查询全部知识库列表及当前默认库
**请求方式**：`GET`

**响应出参**

| 参数名            | 数据类型 | 说明             |
| ----------------- | -------- | ---------------- |
| `success`         | Boolean  | 接口调用结果     |
| `knowledge_bases` | Array    | 知识库列表       |
| `current_kb`      | String   | 当前默认知识库ID |

**knowledge_bases子项结构**

| 字段          | 类型    | 说明         |
| ------------- | ------- | ------------ |
| `id`          | String  | 知识库ID     |
| `name`        | String  | 知识库名称   |
| `description` | String  | 描述信息     |
| `item_count`  | Integer | 条目总数     |
| `created_at`  | String  | 创建时间     |
| `is_default`  | Boolean | 是否为默认库 |

**响应示例**

```json
{
  "success": true,
  "knowledge_bases": [
    {
      "id": "kb_default",
      "name": "默认知识库",
      "description": "系统默认知识库",
      "item_count": 150,
      "created_at": "2024-01-01T00:00:00Z",
      "is_default": true
    },
    {
      "id": "kb_custom",
      "name": "PLC案例库",
      "description": "自定义PLC编程案例",
      "item_count": 50,
      "created_at": "2024-06-01T00:00:00Z",
      "is_default": false
    }
  ],
  "current_kb": "kb_default"
}
```

#### 3.16.2 `/api/knowledge/multi-kb/create`

**接口说明**：新建独立知识库
**请求方式**：`POST`

**请求入参**

| 参数名        | 数据类型 | 是否必填 | 说明               |
| ------------- | -------- | -------- | ------------------ |
| `name`        | String   | 是       | 知识库名称（唯一） |
| `description` | String   | 否       | 知识库描述         |

**响应出参**

| 参数名    | 数据类型 | 说明         |
| --------- | -------- | ------------ |
| `success` | Boolean  | 创建结果     |
| `kb_id`   | String   | 新建知识库ID |
| `message` | String   | 操作提示     |

#### 3.16.3 `/api/knowledge/multi-kb/switch`

**接口说明**：切换系统默认知识库
**请求方式**：`POST`

**请求入参**

| 参数名  | 数据类型 | 是否必填 | 说明         |
| ------- | -------- | -------- | ------------ |
| `kb_id` | String   | 是       | 目标知识库ID |

**响应出参**

| 参数名    | 数据类型 | 说明     |
| --------- | -------- | -------- |
| `success` | Boolean  | 切换结果 |
| `message` | String   | 操作提示 |

#### 3.16.4 `/api/knowledge/multi-kb/current`

**接口说明**：查询当前默认知识库详情
**请求方式**：`GET`

**响应出参**

| 参数名       | 数据类型 | 说明               |
| ------------ | -------- | ------------------ |
| `success`    | Boolean  | 接口调用结果       |
| `current_kb` | Object   | 当前知识库完整信息 |

### 3.17 知识同步任务接口详细设计

#### 3.17.1 `/api/knowledge/sync-tasks`

**接口说明**：查询RAG向量库同步任务列表与进度
**请求方式**：`GET`

**响应出参**

| 参数名    | 数据类型 | 说明         |
| --------- | -------- | ------------ |
| `success` | Boolean  | 接口调用结果 |
| `tasks`   | Array    | 同步任务列表 |
| `total`   | Integer  | 任务总数     |

**tasks子项结构**

| 字段           | 类型   | 说明                                           |
| -------------- | ------ | ---------------------------------------------- |
| `task_id`      | String | 任务ID                                         |
| `status`       | String | 状态：`pending`/`running`/`completed`/`failed` |
| `progress`     | Number | 执行进度0~1                                    |
| `created_at`   | String | 任务创建时间                                   |
| `completed_at` | String | 任务完成时间                                   |
| `message`      | String | 状态描述                                       |
| `error`        | String | 错误信息（失败时返回）                         |

**响应示例**

```json
{
  "success": true,
  "tasks": [
    {
      "task_id": "sync_001",
      "status": "completed",
      "progress": 1.0,
      "created_at": "2024-06-12T10:00:00Z",
      "completed_at": "2024-06-12T10:05:00Z",
      "message": "同步完成，新增150条向量"
    },
    {
      "task_id": "sync_002",
      "status": "running",
      "progress": 0.65,
      "created_at": "2024-06-12T11:00:00Z",
      "completed_at": null,
      "message": "正在同步..."
    }
  ],
  "total": 2
}
```

---

## 五、附录

### 4.1 接口汇总表


| 序号 | 路由 | 方法 | 模块 | 模块详细说明 |
| ---- | -------------------------------------------------- | ------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | /api/chat/stream | POST | 聊天会话 | **流式对话主入口**：核心智能体调度入口，通过 `agent_id` 路由到智能开发/智能修复/形式化验证/智能测试智能体，支持 SSE 流式响应，适用需要实时交互的复杂任务 |
| 2 | /api/chat | POST | 聊天会话 | **非流式对话**：与流式接口功能相同，但返回一次性完整响应，适用于对响应时效要求不高或网络不稳定的场景 |
| 3 | /api/session/abort | POST | 聊天会话 | **全局会话中止**：终止当前正在进行的AI生成内容，支持对话被外部中断后清理状态，释放LLM资源 |
| 4 | /api/session/{session_id}/messages | GET | 聊天会话 | **历史消息获取**：根据会话ID获取该会话的所有消息记录，支持分页，用于前端加载历史上下文 |
| 5 | /api/session/{session_id}/abort | POST | 聊天会话 | **指定会话中止**：通过路径参数精确中止特定会话的内容生成，适合多会话并行时单独管理 |
| 6 | /api/knowledge/qa/sessions/{session_id}/clear | POST | 知识库 | **知识问答会话清空**：清除知识库QA会话的上下文历史，保留会话本身但重置对话状态 |
| 7 | /api/knowledge/chat/stream | POST | 知识库 | **知识库流式问答**：基于上传的文档/知识条目进行RAG检索回答，支持流式输出，适用于文档问答、知识查询场景 |
| 8 | /api/knowledge/chat/enhanced | POST | 知识库 | **增强型知识问答**：在流式问答基础上增强多跳推理、跨文档关联能力，适合复杂知识推理场景 |
| 9 | /api/knowledge/chat/sessions | GET | 知识库 | **知识聊天会话列表**：获取当前用户所有知识聊天会话的元信息（ID、标题、创建时间、最后活跃时间） |
| 10 | /api/knowledge/chat/sessions/{session_id} | GET | 知识库 | **知识聊天会话详情**：获取指定知识会话的完整信息，包括会话配置、关联知识库ID等 |
| 11 | /api/knowledge/chat/sessions/{session_id}/messages | GET | 知识库 | **知识聊天消息历史**：获取指定知识会话的所有问答消息，用于前端渲染或继续对话 |
| 12 | /api/knowledge/chat/sessions | POST | 知识库 | **创建知识聊天会话**：新建一个知识问答会话，需指定关联的知识库或知识条目，返回会话ID |
| 13 | /api/knowledge/chat/sessions/{session_id} | DELETE | 知识库 | **删除知识聊天会话**：永久删除指定的知识会话及其所有消息，不可恢复 |
| 14 | /api/knowledge/chat/sessions/{session_id}/clear | POST | 知识库 | **清空知识聊天上下文**：保留会话结构但清除所有问答历史，等同于开启新的对话轮次 |
| 15 | /api/knowledge/chat/sessions/{session_id}/rename | PUT | 知识库 | **重命名知识聊天会话**：修改知识会话的显示名称，便于用户管理和识别 |
| 16 | /api/knowledge/chat/intelligent | POST | 知识库 | **智能路由知识问答**：系统自动判断使用哪个知识库或是否结合LLM能力，智能选择最优回答路径 |
| 17 | /api/formal-validation/validate | POST | 形式化验证 | **形式化输入预校验**：在正式验证前检查属性描述、代码格式是否满足验证引擎要求，提前发现格式错误 |
| 18 | /api/formal-validation/convert-natural-language | POST | 形式化验证 | **自然语言转形式化属性**：将用户的自然语言需求描述（如"电机停止时抱闸必须闭合"）转换为PLCverif可识别的形式化属性JSON |
| 19 | /api/formal-validation/format-examples | GET | 形式化验证 | **属性格式示例查询**：获取系统支持的形式化属性模板、示例及语法说明，帮助用户正确编写属性 |
| 20 | /api/compilation/validate | POST | 形式化验证 | **编译输入校验**：检查待验证的ST/SCL代码是否为有效的编译器输入，包括语法检查、变量声明检查 |
| 21 | /api/formal/reports/{report_id}.json | GET | 形式化验证 | **形式化报告JSON**：获取指定报告的完整JSON结构，含属性验证结果、反例轨迹、覆盖范围等机器可读数据 |
| 22 | /api/formal/reports/{report_id}.md | GET | 形式化验证 | **形式化报告Markdown**：获取人类可读的Markdown格式报告，适合直接查看、导出或嵌入文档 |
| 23 | /api/formal/reports/{report_id}.html | GET | 形式化验证 | **形式化报告HTML**：获取渲染后的HTML格式报告，支持富文本展示、交互图表，适合Web嵌入 |
| 24 | /api/formal/reports/{report_id}/bundle.zip | GET | 形式化验证 | **形式化报告完整包**：下载包含JSON/MD/HTML报告+相关资源（图片、图表数据）的ZIP压缩包 |
| 25 | /api/fuzz/reports/{report_id}.md | GET | Fuzz测试 | **Fuzz报告Markdown下载**：获取模糊测试结果的Markdown格式报告，含测试用例统计、失败用例详情 |
| 26 | /api/fuzz/reports/{report_id}/bundle.zip | GET | Fuzz测试 | **Fuzz报告完整包下载**：下载包含Markdown报告+所有失败测试用例JSON的ZIP包，便于本地分析 |
| 27 | /api/fuzz/reports/{report_id}/testcases.json | GET | Fuzz测试 | **Fuzz测试用例明细**：获取指定报告下所有测试用例的详细输入输出数据，用于精准复现和修复 |
| 28 | /api/auth/login | POST | 认证账号 | **用户登录**：验证用户名密码，返回JWT访问令牌（access_token）和刷新令牌（refresh_token），触发会话建立 |
| 29 | /api/auth/register | POST | 认证账号 | **用户注册**：创建新用户账号，校验邮箱唯一性，发送邮箱验证链接（可选），初始化用户配置 |
| 30 | /api/auth/validate | GET | 认证账号 | **Token有效性校验**：验证当前访问令牌是否有效、是否过期，返回用户身份信息用于鉴权中间件 |
| 31 | /api/auth/check | GET | 认证账号 | **登录状态检查**：查询当前请求是否携带有效认证信息，返回会话状态（已登录/未登录/已过期） |
| 32 | /api/auth/logout | POST | 认证账号 | **用户登出**：使当前令牌失效，清理服务端会话状态，支持多端同步登出 |
| 33 | /api/auth/change-password | POST | 认证账号 | **修改密码**：验证旧密码后更新为新密码，需确保新密码强度符合策略，强制其他设备重新登录 |
| 34 | /api/auth/forgot-password | POST | 认证账号 | **忘记密码申请**：输入注册邮箱或手机号，系统发送验证码/重置链接，开启密码重置流程 |
| 35 | /api/auth/verify-code | POST | 认证账号 | **验证码校验**：验证用户提交的邮箱/短信验证码有效性，通常有时效限制（5-10分钟） |
| 36 | /api/auth/reset-password | POST | 认证账号 | **密码重置**：使用有效的验证码+Token组合设置新密码，完成密码找回流程 |
| 37 | /api/account/info | GET | 认证账号 | **账号信息查询**：获取当前登录账号的详细信息，包括昵称、邮箱、注册时间、账号状态 |
| 38 | /api/account/info | PUT | 认证账号 | **账号信息更新**：修改账号的展示信息（如昵称、头像、个人简介），不影响登录认证信息 |
| 39 | /api/user/info | GET | 认证账号 | **用户资料查询**：获取用户个人资料，可能包含扩展信息如组织、角色、权限列表 |
| 40 | /api/user/info | PUT | 认证账号 | **用户资料更新**：更新用户个人资料，支持修改姓名、联系方式、部门等扩展属性 |
| 41 | /api/upload | POST | 文件 | **单文件上传**：上传PLC代码文件、配置文件或其他二进制文件，返回 file_id 用于后续访问，支持 multipart/form-data |
| 42 | /api/upload/multiple | POST | 文件 | **批量文件上传**：一次性上传多个文件，适用于上传整个项目文件夹或多个依赖库文件 |
| 43 | /api/files/{file_id}/download | GET | 文件 | **文件下载**：通过 file_id 下载已上传的文件内容，支持断点续传，适用于大型ST/SCL文件 |
| 44 | /api/files/{file_id}/view | GET | 文件 | **文件在线预览**：不下载直接在线查看文件内容，支持代码高亮、语法识别，适合Web端快速预览 |
| 45 | /health | GET | 健康检查 | **基础健康检查**：检测服务进程存活状态，无需认证，用于负载均衡探活或K8s存活探测 |
| 46 | /api/health | GET | 健康检查 | **全链路健康检查**：检测服务及所有依赖（数据库、Redis、外部API）的健康状态，返回详细诊断信息 |
| 47 | /api/smart_dev/switch_language | POST | 智能开发 | **开发语言切换**：在多语言开发过程中动态切换目标编程语言（如从ST切换到SCL），重置上下文 |
| 48 | /api/smart_dev/generate | POST | 智能开发 | **智能代码生成**：基于自然语言需求或模板生成ST/SCL/FBD代码，是智能开发模块的核心接口 |
| 49 | /api/smart_dev/languages | GET | 智能开发 | **支持的语言列表**：查询系统当前支持的PLC编程语言列表及版本信息 |
| 50 | /api/smart_dev/templates | GET | 智能开发 | **代码模板列表**：获取预置的代码模板（如启停控制、定时器、PID），支持快速初始化项目 |

#### 内部私有/场景可选接口（详细模块说明）

| 路由 | 方法 | 模块 | 模块详细说明 |
| -------------------------------- | ------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| /api/chat/history/item/update | POST | 聊天会话 | **聊天历史条目更新**：更新指定历史消息的元数据（如标记收藏、添加备注），不修改消息内容 |
| /api/message/save | POST | 聊天会话 | **消息保存**：将单条消息持久化到数据库，通常配合流式响应使用，确保消息不丢失 |
| /api/chat/history | DELETE | 聊天会话 | **聊天历史批量删除**：按条件批量删除聊天历史，支持按时间范围、会话ID等过滤 |
| /api/session/status/{session_id} | GET | 聊天会话 | **会话状态查询**：查询指定会话的实时状态（空闲/生成中/已完成/已中止），用于前端状态同步 |
| /api/session/pin | POST | 聊天会话 | **会话置顶/取消置顶**：将重要会话固定在列表顶部，支持切换状态 |
| /api/session/share | POST | 聊天会话 | **会话分享**：生成一次性或永久分享链接，允许他人查看会话内容（脱敏处理后） |
| /api/shared/{share_id} | GET | 聊天会话 | **查看分享的会话**：通过分享ID获取会话内容，支持访问控制和过期机制 |
| /api/session/share/{share_id} | DELETE | 聊天会话 | **取消会话分享**：删除已分享的链接，使分享内容立即失效 |
| /api/session/shares | GET | 聊天会话 | **我的分享列表**：获取当前用户所有已分享的会话链接及访问统计 |
| /api/session/stats | GET | 聊天会话 | **会话统计信息**：获取会话使用数据，如总消息数、Token消耗、活跃时段等 |
| /api/knowledge/chat/diagnose | GET | 知识库 | **知识聊天诊断**：检查知识检索链路健康状态，返回检索质量评分、召回率等诊断信息 |
| /api/knowledge/import-oscat | POST | 知识库 | **OSCAT标准库导入**：批量导入OSCAT开源PLC函数库到知识库，支持搜索和代码片段引用 |
| /api/knowledge/reader/item/{knowledge_id} | GET | 知识库 | **知识条目阅读视图**：以页面形式展示单个知识条目的完整内容，支持目录导航 |
| /api/knowledge/reader/chunks/{knowledge_id} | GET | 知识库 | **知识分块阅读**：展示知识条目切分后的所有chunk片段，用于调试检索召回质量 |
| /api/knowledge/reader/quality-score | GET | 知识库 | **知识质量评分**：评估知识库内容的完整性、准确性、可检索性，返回综合质量分数 |
| /api/knowledge/sync-tasks | GET | 知识库 | **知识同步任务列表**：查看增量同步、外部数据源同步的任务状态和进度 |
| /api/pou/recommend | POST | 文件/POU | **POU推荐**：根据当前代码上下文推荐适合的编程组织单元（Program Organization Unit）结构 |
| /api/pou/extract/{file_id} | GET | 文件/POU | **POU数据提取**：从上传的PLC文件中解析并提取POU（函数、功能块、程序）结构信息 |
| /api/tasks | GET | 任务 | **任务列表查询**：查看异步任务（如长时间编译、验证、生成）的队列状态和进度 |
| /api/tasks/{task_id} | GET | 任务 | **任务详情查询**：获取指定任务的完整执行信息，包括输入参数、输出结果、错误日志 |
| /api/payment/create-order | POST | 支付 | **创建支付订单**：生成待支付订单记录，关联商品/服务，返回支付跳转所需信息 |
| /api/payment/orders | GET | 支付 | **订单列表查询**：获取当前用户的全部订单，支持按状态（待支付/已支付/已取消）筛选 |
| /api/payment/order/{order_id} | GET | 支付 | **订单详情查询**：获取指定订单的完整信息，包括支付流水、退款记录等 |
| /api/payment/pay/{order_id} | POST | 支付 | **发起支付**：调起支付渠道（支付宝/微信/银行卡），返回支付表单或跳转链接 |
| /api/payment/confirm/{order_id} | POST | 支付 | **支付结果确认**：接收支付渠道回调，确认订单支付状态，完成订单闭环 |
| /api/payment/supported-methods | GET | 支付 | **支持的支付方式**：查询当前系统支持的支付渠道列表及限额信息 |
| /api/payment/billing/list | GET | 支付 | **账单列表查询**：获取用户的消费账单，支持按月/季度汇总，含详细计费项 |

### 4.2 变更记录

| 版本 | 日期       | 变更内容                       | 变更人 |
| ---- | ---------- | ------------------------------ | ------ |
| 1.0  | 2024-01-01 | 初始版本                       | -      |
| 2.0  | 2026-06-12 | 整合所有接口文档，补全详细设计 | -      |
