已对照本地 [接口文档](/home/gargantua/code/morrow_py/docs/plc_subagent_api.md:302)、[形式化验证文档](/home/gargantua/code/morrow_py/docs/形式化验证文档.md:22) 和 [输入样例](/home/gargantua/code/morrow_py/docs/各个subagent输入样例.md:1)，在 `2026-07-17` 对 `http://60.188.37.6:28080` 做了实际调用、错误输入、枚举和边界测试。

### 核心结论

标准入口应使用：

```http
POST /api/chat/stream
Content-Type: application/json
```

当前真正可用的 4 个 agent ID 是：

| Agent | 正确 `agent_id` | 当前状态 |
|---|---|---|
| 智能开发 | `retrieval_planning_coding_agent` | ST、SCL 可用；FBD 故障 |
| 智能修复 | `compilation_debugging_agent` | 编译修复可用；测试/形式化修复有故障 |
| 形式化验证 | `formal_validation_agent` | 标准结构化输入可用 |
| 智能测试 | `fuzz_testing_agent` | 可运行，但统一入口强制使用 `legacy` 方法 |

`plc_dev`、`plc_test`、`plc_repair`、`plc_formal` 不是合法 ID。更危险的是：它们在流式接口中不会可靠报错，而可能静默回退到智能开发工作流。

### 有效性统计

| 范围 | 测试结果 |
|---|---|
| 文档列出的 6 个 agent ID | 4 个工作流可调用；`enhanced_super_agent`、`single_agent_llm` 当前均返回无可用服务 |
| 4 个 `plc_*` 简写 ID | 0 个合法；流式请求会错误回退到开发工作流 |
| 开发语言 | 3 个被接受；ST/SCL 2 个可运行，FBD 1 个因缺模块不可运行 |
| 编译器 | 文档列 2 个；仅确认 `matiec` 环境可用，Rusty 当前未安装 |
| 修复模式 | 4 类可路由；只有编译修复可靠工作 |
| 形式化 `job_req` | `assertion`、`pattern` 共 2 个，均验证成功过 |
| 形式化 `pattern_id` | `pattern-invariant`、`pattern-implication`、`pattern-forbidden` 共 3 个，均被识别 |
| 形式化文档 9 种输入方式 | 9 种都能进入工作流，但只有标准数组和单属性对象能可靠保留语义 |
| `/api/fuzz/methods` 当前枚举 | 5 个；3 个可运行，2 个缺依赖 |
| 旧文档 Fuzz 值 | `boundary/scenario/coverage/property_based/llm` 5 个全部无效 |
| 统一测试 agent 的 `fuzz_method` | 测试 8 种输入，0 种真正生效，报告始终是 `legacy` |

## 通用请求格式

```json
{
  "message": "必填字符串",
  "agent_id": "精确的 agent ID",
  "session_id": "可选，自定义或复用会话",
  "user_id": "可选，缺省为 default_user",
  "language": "zh-CN",
  "context": {},
  "uploaded_files": []
}
```

实际字段及注意事项：

| 字段 | 实际规则 |
|---|---|
| `message` | 唯一必填字段；空字符串也会被接受，不建议使用 |
| `agent_id` | 可省略，但会默认到当前不可用的超级 agent；必须显式传精确值 |
| `session_id` | 可选字符串；传入后服务原样使用 |
| `user_id` | 缺省为 `default_user`；不能传 `null` |
| `language` | UI 语言，不是 PLC 语言；不能传 `null` |
| `context` | 必须为对象或 `null`；内部字段没有严格类型/枚举校验 |
| `uploaded_files` | 必须为对象数组；内联 `content`/`extracted_text` 未被识别为 ST 输入 |

以下驼峰字段会被静默忽略：

```text
sessionId
agentId
userId
uploadedFiles
```

必须使用 snake_case。

当前实例未强制要求 `Authorization`，所有测试均未携带 Token。`Content-Type: application/json` 则是必需的。

成功响应是 SSE：

```text
data: {"type":"session_id", ...}

data: {"type":"agent_start", ...}

data: {"type":"token", ...}
```

不能只等待 `done` 或 `complete`：

- 成功工作流通常结束于 `workflow_end`
- 部分流程只返回 `stage_guidance`、`spec_generated` 或直接断开
- 某些错误被包装成 `token`，而不是 `error`
- HTTP 200 不代表业务成功

非流式 `/api/chat` 不适合作为标准入口。智能修复实测返回：

```json
{
  "success": true,
  "response": "",
  "agent_id": "compilation_debugging_agent",
  "real_agent_used": true
}
```

结构化报告全部丢失。

## 1. 智能开发

标准调用：

```json
{
  "message": "直接生成一个最小可编译的 ST FUNCTION_BLOCK：输入 x，输出 y，执行 y := x。",
  "agent_id": "retrieval_planning_coding_agent",
  "user_id": "your_user_id",
  "context": {
    "target_language": "ST",
    "compiler_type": "matiec",
    "enable_socratic_spec": false
  }
}
```

### 参数结果

| 参数 | 推荐值 | 实测情况 |
|---|---|---|
| `target_language` | `ST`、`SCL` | ST、SCL 成功 |
|  | `FBD` | 路由被识别，但报 `No module named 'tools.xml_generator_tool'` |
|  | 缺省 | 默认 ST |
|  | `st`、`XYZ` | 均被接受并原样写入输出，仍生成、编译 ST；没有枚举校验 |
| `compiler_type` | `matiec` | 当前环境确认可用 |
|  | `rusty` | 请求被接受，但预检显示 Rusty 不存在，无法确认真正使用 |
|  | 任意非法值 | 也会被接受 |
| `enable_socratic_spec` | `false` | 直接开发 |
|  | `true` | 进入单独的规格书/问答回合，不直接生成代码 |
| `socratic_skip` | `true` | 在 `enable_socratic_spec=true` 时未成功跳过 |
| `socratic_spec_md` | Markdown 字符串 | 第二回合传入后可进入编码阶段 |
| `rpc_pipeline` | 不传 | 生成并编译后结束 |
|  | `["fuzz"]` | 自动执行测试，成功 |
|  | `["formal"]` | 只生成属性，达到内部迭代上限，未执行验证 |
|  | `["fuzz","formal"]` | 只完成 Fuzz，形式化验证未运行 |
| `template` | 文档值如 `pid` | 当前模板接口 404，未观察到可靠作用 |
| `language_hint` | 任意字符串 | 当前前端不发送，未观察到路由作用 |

建议形式化验证不要放在开发 `rpc_pipeline` 内，应独立调用形式化 agent。

### 标准输出

ST/SCL 返回：

```json
{
  "type": "st_code_json",
  "stCode": {
    "code": "FUNCTION_BLOCK ...",
    "file_name": "st_file_....st",
    "language": "ST"
  },
  "content": {
    "code": "FUNCTION_BLOCK ...",
    "file_path": "...",
    "file_name": "...",
    "language": "ST"
  }
}
```

SCL 虽然 `language="SCL"`，文件扩展名实测仍为 `.st`。

FBD 当前会返回错误 `token`，随后却仍返回：

```json
{"type":"workflow_end","content":"✅ 工作流完成"}
```

因此必须扫描错误 token，不能只看结束事件。

## 2. 智能修复

最稳定的输入方式是把 ST 输入对象序列化到 `message`：

```javascript
const request = {
  message: JSON.stringify({
    st_code: sourceCode
  }),
  agent_id: "compilation_debugging_agent",
  context: {
    repair_source: "compile",
    compiler_type: "matiec",
    repair_failure_notes: "缺少分号并误用了等号"
  }
};
```

`context.st_code` 单独传入会被忽略。

### 修复模式

`repair_source` 实测：

| 值 | 实际路由 | 当前结果 |
|---|---|---|
| `compile` | 编译修复 | 可用 |
| `test_failure` | 测试失败修复 | 进入对应模式，但因异步代码错误失败 |
| `formal_validation_failure` | 反例修复 | 可进入 LLM 修复，但缺验证上下文时最终失败 |
| `multi` | 实际回退到编译修复 | 与文档不符 |
| 非法值 | 回退到编译修复 | 不会报参数错误 |

当前前端实际发送的是 `repair_targets`：

| `repair_targets` | 映射结果 |
|---|---|
| `[]`、`["compile"]`、`["syntax"]` | `compile` |
| `["test"]` | `test_failure` |
| `["formal"]` | `formal_validation_failure` |
| `["compile","test"]` | `test_failure` |
| `["test","formal"]` | `multi` |
| 非法目标 | 回退 `compile` |

测试失败修复当前稳定复现：

```text
'async for' requires an object with __aiter__ method, got coroutine
```

错误会重复 5 次，然后返回失败报告。

### 标准输出

```json
{
  "type": "compilation_report_json",
  "content": {
    "workflow_success": true,
    "compilation_success": true,
    "code_file": "st_code_from_input_..._rule_fixed.st",
    "error_count": 0,
    "errors": [],
    "report_id": "compile_...",
    "attempt_count": 0,
    "max_attempts": 3,
    "repair_mode": "compile",
    "repair_mode_label": "编译错误修复"
  }
}
```

关键缺陷：返回值只有修复后文件名，没有修复后代码正文，也没有可用的编译报告下载接口。会话历史中同样没有完整修复代码。

## 3. 形式化验证

标准输入必须放进 `message` 字符串，不应放在 `context.properties`：

```javascript
const formalInput = {
  st_code: sourceCode,
  properties: [
    {
      property_description: "x 为 TRUE 时 y 必须为 TRUE",
      property: {
        job_req: "pattern",
        pattern_id: "pattern-implication",
        pattern_params: {
          "1": "instance.x = TRUE",
          "2": "instance.y = TRUE"
        },
        entry_point: "FB_Twin"
      }
    }
  ]
};

const request = {
  message: JSON.stringify(formalInput),
  agent_id: "formal_validation_agent",
  context: {}
};
```

把相同属性放入 `context.properties` 时，实测被忽略，并自动生成了 4 个其他属性。

### 属性参数

| 参数 | 有效值 |
|---|---|
| `job_req` | `assertion`、`pattern` |
| `pattern_id` | `pattern-invariant`、`pattern-implication`、`pattern-forbidden` |
| invariant 参数 | `pattern_params["1"]` |
| implication 参数 | `pattern_params["1"]` 和 `["2"]` |
| forbidden 参数 | `pattern_params["1"]` |
| `entry_point` | ST 中真实存在的 POU 名称 |
| assertion 内容 | 必须写在 ST 中，例如 `//#ASSERT y = x : label` |

实测结果：

- `assertion`：通过
- `pattern-implication`：通过
- `pattern-forbidden`：通过
- 布尔等式 `pattern-invariant`：被改写后曾因 CBMC `timeout/no_suitable_files` 返回 `NOT_CHECKED`
- 非法 `job_req/pattern_id`：没有干净报错，而是生成 0 属性报告并多次重复验证

### 文档 9 种输入方式

| 输入方式 | 接受 | 语义可靠性 |
|---|---:|---|
| 标准属性数组 | 是 | 推荐 |
| 简写 `property:"safety"` | 是 | 不安全，实测被转换成 `y = y` 并错误 PASS |
| 仅 `description` | 是 | 同样可能生成恒真属性 |
| `properties` 纯文本 | 是 | 同样可能生成恒真属性 |
| 只给 ST | 是 | 调用属性 agent，生成数量不固定，观察到 3～4 条 |
| `natural_language_properties` | 是 | 调用属性 agent，结果不稳定 |
| `property_requirements` | 是 | 调用属性 agent，结果不稳定 |
| JSON 属性 + 自然语言补充 | 是 | 自然语言补充被忽略，只验证 JSON 部分 |
| 单属性对象 | 是 | 会自动包装成数组，推荐 |

因此，9 种格式虽然都能进入工作流，但只有“标准属性数组”和“单个标准属性对象”适合生产调用。

### 标准输出

```json
{
  "type": "formal_report_json",
  "content": {
    "all_satisfied": true,
    "property_count": 1,
    "passed": 1,
    "failed": 0,
    "not_checked": 0,
    "properties": [
      {
        "status": "PASS",
        "job_req": "pattern",
        "pattern_id": "pattern-implication",
        "pattern_params": {
          "1": "x",
          "2": "y"
        },
        "entry_point": "FB_Twin"
      }
    ],
    "report_id": "formal_...",
    "artifacts": {
      "download_json_url": "...",
      "download_md_url": "...",
      "download_html_url": "...",
      "download_bundle_url": "..."
    }
  }
}
```

## 4. 智能测试

推荐输入：

```javascript
const request = {
  message: JSON.stringify({
    st_code: sourceCode
  }),
  agent_id: "fuzz_testing_agent",
  context: {
    fuzz_method: "random",
    case_count: 10
  }
};
```

同样，`context.st_code` 单独传入无效。实测会导致 agent 先生成一段完全不同的测试程序，再测试生成出来的程序。

### 统一 agent 参数

`fuzz_method` 实测传入：

```text
random
afl
dse
boundary
direct_llm
RANDOM
not_a_method
省略
```

8 种情况最终报告全部为：

```json
"fuzz_method": "legacy"
```

因此当前统一 agent 的 `fuzz_method` 参数没有实际控制效果。

`case_count`：

| 输入 | 实际值 |
|---|---:|
| 省略、`null` | 10 |
| `0`、负数、`false` | 1 |
| `1.5` | 1 |
| `"2"` | 2 |
| 正整数 | 按值执行 |
| 大于 100 | 会进入长时间执行，没有观察到快速截断；不建议 |

`enable_fuzz_test:false` 也不会关闭已显式选择的测试 agent。

### 标准输出

```json
{
  "type": "fuzz_report_json",
  "content": {
    "report_id": "fuzz_...",
    "execution_backend": "real",
    "compile_backend": "matiec",
    "fuzz_method": "legacy",
    "config": {
      "requested_case_count": 10
    },
    "summary": {
      "total_test_cases": 10,
      "success_cases": 10,
      "failed_cases": 0,
      "success_rate_pct": 100.0
    },
    "coverage_statistics": {},
    "failed_details": [],
    "generated_testcases": [],
    "rq_metrics": {},
    "artifacts": {}
  }
}
```

### 独立 Fuzz API

如果需要真正控制测试方法，应优先考虑独立接口。

`GET /api/fuzz/methods` 当前返回：

| 方法 | 枚举有效 | 当前可运行 |
|---|---:|---:|
| `random` | 是 | 是 |
| `afl` | 是 | 是 |
| `legacy` | 是 | 是 |
| `dse` | 是 | 否，缺 `baselines` |
| `direct_llm` | 是 | 否，缺 `baselines` |

旧文档值 `boundary`、`scenario`、`coverage`、`property_based`、`llm` 全部返回错误。

`POST /api/fuzz/generate`：

- `method` 缺省为 `random`
- 大小写不敏感，但不清理首尾空格
- `case_count` 默认 50
- 1～100 正常
- 大于 100 截断为 100
- 0/负数返回 `success:false`
- 数字字符串会转换为整数

`POST /api/fuzz/run` 的真实参数位于 query，不是 JSON body：

```http
POST /api/fuzz/run?message=<ST代码>&context={"fuzz_method":"random","case_count":1}
```

该接口实测真正使用了 `random`，与统一测试 agent 不同。

## 模型及通用采样参数

`GET /api/models` 当前返回 15 个模型：

| `model_id` | temperature | top_p | top_k | max_tokens |
|---|---:|---:|---:|---:|
| `qwen3:4b` | 0.7 | 0.9 | 40 | 10000 |
| `qwen2.5:7b` | 0.7 | 0.9 | 40 | 8000 |
| `llama3.1:8b` | 0.7 | 0.9 | 40 | 8000 |
| `Agents4PLC` | 0.7 | 0.9 | 40 | 10000 |
| `deepseek-chat` | 0.7 | 0.9 | 40 | 8000 |
| `gpt-5.4` | 0.2 | 0.9 | 40 | 8000 |
| `gemini-2.5-pro` | 0.2 | 0.9 | 40 | 12000 |
| `glm-5.1` | 0.15 | 0.9 | 40 | 12000 |
| `kimi-k2.5-thinking` | 0.7 | 0.9 | 40 | 8000 |
| `kimi-k2` | 0.25 | 0.9 | 40 | 1024 |
| `kimi-k2.5` | 0.25 | 0.9 | 40 | 1024 |
| `glm-5` | 0.25 | 0.9 | 40 | 1024 |
| `deepseek-v3.2` | 0.25 | 0.9 | 40 | 1024 |
| `qwen3.5-397b-a17b` | 0.25 | 0.9 | 40 | 1024 |
| `doubao-seed-2-0-code-preview-260215` | 0.25 | 0.9 | 40 | 1024 |

注意：

- `gpt-5.4` 在模型列表中明确标注“渠道无此模型”
- 非法 `model_id` 在 FBD 测试中静默回退到 `qwen3:4b`
- 未指定模型时，FBD 默认选择 `glm-5.1`
- `/api/models/not-a-model/params` 仍返回默认参数，不会 404
- 文档中的 `temperature 0～2`、`top_p 0～1` 没有服务端范围校验
- 超范围数值会继续传递
- `temperature/top_p/top_k/max_tokens` 传字符串会直接产生 HTTP 500

## 辅助接口与文档漂移

| 接口 | 实测 |
|---|---|
| `/api/chat/stream` | 主要可用入口 |
| `/api/chat` | 工作流结构化结果丢失 |
| `/api/models` | 可用 |
| `/api/models/{id}/params` | 可用，但不校验 ID |
| `/api/smart_dev/generate` | 404 |
| `/api/smart_dev/switch_language` | 404 |
| `/api/smart_dev/languages` | 404 |
| `/api/smart_dev/templates` | 404 |
| `/api/formal-validation/validate` | 可访问，但请求结构与完整 agent 不同 |
| `/api/formal-validation/convert-natural-language` | 可访问，但经常生成空 `expr`，编号会被错误拆成独立属性 |
| `/api/compilation/validate` | 仅做很弱的文本检查；明显语法错误仍可能 `is_valid:true` |
| `/api/fuzz/methods` | 可用 |
| `/api/fuzz/preflight` | 可用 |
| `/api/fuzz/generate` | 可用 |
| `/api/fuzz/run` | 可用，但参数在 query |
| Fuzz 4 种报告下载 | 4/4 返回 200 |
| Formal 4 种报告下载 | 4/4 返回 200 |
| OpenAPI/Swagger | 未公开；`/openapi.json` 返回前端页面，`/api/openapi.json` 404 |

会话接口另有不一致：

- `/api/session/{id}/messages` 额外要求 query 参数 `user_id` 和 `agent_id`
- `workflow_end` 后 `/api/session/status/{id}` 仍可能显示 `running`
- `message_count` 可能为 0，但消息查询实际返回 2 条
- `/api/session/abort` 可以把状态更新成 `aborted`
