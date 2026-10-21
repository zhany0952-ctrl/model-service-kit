# 模型服务包公共接口

本页说明`model-service-kit 0.1.0`的通用配置、凭据、GUI及流式调用接口。普通wheel、安装／迁移及独立项目接入见[安装手册](installation.md)和[接入说明](integration.md)。本包不包含NPC适配器，不以NPC认知测试代替通用接口保证；不自动公开发布。下方较早日期说明接口的引入时间，功能以当前源码和验证记录为准。

## 已确认的目标接口

模块提供图形化配置与模型API调用，不理解NPC区域、连接、提示词或业务验证。应用内调用，不增加Gateway Server。

| 能力 | 目标行为 |
| --- | --- |
| 配置管理 | 新增、编辑、删除、选择活动配置；获取不可变配置快照。 |
| 配置内容 | 显示名称、服务地址、模型名称、凭据引用；温度、输出上限和服务支持的思考设置可选，省略采用服务默认。连接／读取超时独立于调用方性能标准。 |
| 生成 | 输入消息和固定配置，返回本地调用句柄与流式事件；正文增量立即提供给调用方。 |
| 正文增量 | 仅新增正文；推理专用字段不混入正文，调用方不能据其判断业务阶段完成。 |
| 调用结束 | 区分正常、长度截断、服务明确拒绝、本地取消；保留服务原始结束原因。 |
| 调用故障 | 网络、超时、HTTP或协议错误及直接信息；连接关闭不自动视为正常完成。 |
| 停止 | 停止本地接收、释放连接并反馈本地结果，不承诺远端生成已停止。 |
| 用量 | 有则记录，不作为业务交付前提。 |

不自动重试或切换模型。流式调用契约见下节；目标表不是所有能力已经交付的声明。当前可调用start／events／cancel及新版配置／凭据接口，已有短消息及NPC侧真实初测；正式认知性能验收仍未完成。M1采用多个独立区域JSON；start不发送response_format，不负责拼装或验证业务对象。旧generate保留同步调用兼容，已改用凭据引用。

### HTTP客户端生命周期（2026-10-16）

ModelService在第一次实际请求时创建HTTPX异步客户端，在同一服务对象、同一事件循环内复用连接池。使用async with ModelService()或在退出前await service.aclose()；关闭后不能再start，不能跨线程／事件循环使用同一异步服务对象。aclose可重复／并发等待，会先取消仍在途的调用，再关闭整个池。旧同步generate不使用该异步池。

每个start仍有独立请求、凭据头、超时、句柄和终态；cancel只终止该调用及释放该响应，不关闭服务或其他调用。正常响应须完整读到HTTP响应体结束，才安全回池；DONE后仍有事件视为协议错误，响应不结束仍受读取空闲／调用方总时限约束，不为复用无限等待。响应结束不再等同于物理TCP断开。

客户端不跟随重定向、不读环境代理，不持久保存Cookie；认证头不设为客户端默认值。复用连接不复用消息、上下文或模型记忆，也不削弱配置快照隔离。通用配置GUI的独立短消息测试每次创建自己的服务及线程事件循环，退出时显式关池，不把池跨线程传递。NPC认知测试窗口自2026-10-18起跨任务复用自己后台线程所属的服务，详见下节，不改变短消息测试的独立生命周期。

### 3A跨任务复用与可选诊断（2026-10-18）

`ModelService(..., keepalive_expiry_s=5.0)`新增可选的客户端空闲保留秒数，必须为正的有限值；默认保持HTTPX原有5秒。NPC测试窗口使用300秒，以便人工查看后续轮仍有机会复用连接。服务端可自行更早关闭；保留期不是连接成功保证，也不是模型上下文保存期。窗口内一个后台线程／事件循环／服务，关闭窗口显式关池，取消只停止本次任务。

开启`diagnostics=True`时，`handle.diagnostics()`新增以下只读补充；所有嵌套对象均返回副本，关闭诊断时不安装trace回调或采集缓存量，原有`events()`及终态`usage`三字段契约不变：

| 字段 | 证据与边界 |
| --- | --- |
| `started_monotonic_s` | 本机单调时钟起点；用于调用方换算自己的计时原点，不是服务端时间或UTC时间。 |
| `transport_stats` | 白名单trace计数：`trace_events`、`tcp_connects`、`tls_handshakes`、`requests_started`。确实发送请求且没有新建TCP才构成复用证据；MockTransport没有trace时不能据0断言已复用。 |
| `timeline`新增阶段 | TCP开始／完成／失败、TLS开始／完成／失败、请求头发送、请求体发送、等待／接收响应头。每类阶段保留首次；不复制trace的`info`、地址、头、异常或证书。TCP阶段含域名解析等待，无法单独分出DNS耗时。 |
| `cache_usage` | 仅在服务提供有效非负整数时保留`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`及`prompt_tokens_details.cached_tokens`映射的`cached_tokens`。没有字段就省略，不填0，不存其他usage字段。 |
| `diagnostic_warnings` | 可选的固定代号`invalid_cache_usage`／`conflicting_cache_usage`，标记可选诊断异常；不把坏缓存统计升级为正文业务失败，不隐去有效实际计数。 |

缓存统计只表明供应商复用了部分输入计算，不能证明存在前轮对话记忆、复用了输出，或确定纯模型计算耗时。缓存由供应商自动管理、并非100%命中，不通过修改消息或缓存量制造优化收益。[DeepSeek缓存说明](https://api-docs.deepseek.com/guides/kv_cache/)、[用量字段](https://api-docs.deepseek.com/api/create-chat-completion/)、[HTTPCore trace接口](https://www.encode.io/httpcore/extensions/)。

通用包仍不写诊断文件。NPC测试适配器仅在启用接收计时的测试路径，把数值诊断作为独立`model_diagnostics`事件关联实际call／attempt保存；它不进入认知输入或C7验收，也不会为等待尾部缓存量延迟C7。取消早于用量到达时，缺失统计保持未知。这类业务适配器不包含在通用wheel中。

凭据方案：界面输入API Key，保存到本机系统凭据库，普通配置文件仅存引用；无凭据库时可仅在本次进程使用，不静默降级为明文文件。普通配置可迁移，凭据在目标设备重新配置，不进入运行快照、日志、源码或wheel。

分发方案：开发使用可编辑安装；跨设备或其他项目使用普通wheel，安装到目标项目自行选择的Python环境，不绑定历史盘符。wheel不包含Python环境，目标设备须具备Python、依赖和GUI所需Tk。独立采用SemVer，首次交付计划0.1.0，不随NPC子版本同步递增。打包／安装说明见[README](../README.md)。

### GUI收尾与独立连接测试（2026-10-15）

GUI显示当前配置文件路径，支持选择／新建配置路径、中文帮助和字段／按钮悬停说明。新建文件不覆盖旧文件，未保存输入须确认后才丢弃。输入新密钥与清除引用互斥；系统库／仅进程保存及普通参数明文的边界保持不变。

连接测试是用户明确确认的独立请求，不是保存或设为活动时的自动动作。只使用已保存且与表单一致的配置快照；单条固定短消息，无NPC人设、历史、记忆或会话引用。本次上限取保存值与64的较小值（未填则64），保留其他生成设置，不修改配置。总时限30秒，原连接／读取空闲超时仍生效；不重试。后台线程处理网络，Tk主线程读取结果队列，允许取消和关闭时释放连接。

非空正文加可靠normal终止才通过，不要求逐字回复；截断、拒绝、空正文、网络／HTTP／协议故障和超时均不算通过。记录首片、总耗时与字符数，默认不采集生成内容，可选预览见下文；不写NPC状态或更改活动配置。取消只代表本地停止接收。界面结果不代表模型认知质量、性能基准或所有服务兼容；测试服务商是否保留请求由服务商政策决定。操作步骤见[GUI使用手册](../README.md#独立连接测试)。

2026-10-15诊断补充：右侧独立面板实时显示实际传输阶段和里程碑时间线。start增加可选diagnostics=false参数；仅显式启用时，handle.diagnostics()返回独立累计快照，与events正文／结束／故障事件分开，不改变原有事件字段或终止语义。

快照包含stage、elapsed_s、timeline、received_bytes／network_chunks、event_count、text_chars／text_chunks和reasoning_chars／reasoning_chunks；实际取得后才包含http_status、白名单content_type、raw_reason及安全error_type。终止后保存last_stage和terminal_kind／error_code／reason，不把本地总时限取消误称为服务报错。timeline每类里程碑只记一次，界面队列只保存最新累计快照，避免无限积压。思考仅统计服务明确分离的字符串字段，不保存内容，也不从正文标签反推思考。

ConnectTimeout／ReadTimeout等类型可区分，HTTP状态配固定排查建议，不复制原始异常、响应头／错误体或地址。没有HTTP响应、没有响应体、已有思考但无正文等情况保留不同证据；服务端加载、排队及推理内部阶段仍不可由客户端断言。独立测试总时限和输出上限保持30秒／64 token，不自动重试或改变模型设置。诊断不写文件或NPC运行快照，不是正式性能基准。

2026-10-15可选内容与思考预设补充：GUI的默认／请求开启／请求关闭／自定义JSON选择复用thinking_json，不新增持久化字段。开启／关闭分别写入reasoning_effort=medium／none，保存后用于实际请求；是否支持由服务决定，不静默降级。默认清空思考JSON，自定义内容先确认；其他参数保留与冲突处理见[手册](../README.md#思考请求开关服务模型支持时)。

start增加capture_content=false可选参数，开启后handle.preview()返回text、reasoning及truncated的有限内存副本；关闭时返回None且不额外保留内容。每类最多4096字符，已知API Key原值及尾部分片在读取预览时遮蔽；不保证过滤所有敏感内容。preview与diagnostics、events分离，不改变正文事件，不把服务思考字段交给NPC区域解析器。

独立测试在开始前显式选择采集；UI线程通过另一份有界队列取得预览，结果对象的preview字段不参与repr，日志仍只含数值及安全阶段。预览不写配置、时间线或NPC状态；取消勾选清空显示，新测试清空旧内容，关闭后不保留。首个事件里程碑仅记录首次，尾部用量不再覆盖结束前阶段。思考开关提示只依可观测字段，不声称已验证模型内部计算方式。

## 配置与凭据的当前接口（2026-10-15）

配置文件采用schema_version=2，根对象为schema_version、active_profile_id、profiles；不接受额外根字段或明文api_key。ModelProfile为冻结数据类，asdict／repr不含密钥值；思考参数保存为不可变JSON文本，每次生成参数返回独立字典。

| 配置字段 | 规则 |
| --- | --- |
| profile_id／name／base_url／model | 非空；地址不含用户名、密码、查询或片段；有凭据的远端服务要求HTTPS，本机回环允许HTTP。 |
| credential_ref | 可空；只存keyring或session随机引用。新密钥生成新引用，不覆盖旧引用。 |
| connect_timeout_seconds／read_timeout_seconds | 正的有限秒数，默认10／60；start显式传入的预算优先，用于后续NPC剩余时间对齐。 |
| temperature | 可空；提供时为0～2有限数值，不自动判断所选模型是否支持。 |
| max_output_tokens／token_limit_field | 上限可空或正整数；明确选择max_completion_tokens或max_tokens，不自动猜测服务差异，不同时发送两个字段。 |
| thinking_json | 可空或标准JSON对象文本，按服务文档填写；禁止覆盖消息／流协议／独立参数或携带认证字段；不自动启用思考。 |
| timeout_seconds／json_mode | 为旧同步generate保留；新GUI创建的配置关闭旧JSON模式，流式入口始终忽略该模式。 |

| 接口 | 当前行为 |
| --- | --- |
| ProfileStore.list_profiles／get／active_profile | 读取配置快照，不读取凭据库密钥；已取得快照不随配置文件改变。 |
| save(profile, api_key=None, credential_mode="system", clear_key=False) | 保存元数据；提供新密钥才新建凭据；编辑时不提供新密钥保留原引用，清除必须显式clear_key。返回实际保存的快照。 |
| activate／delete | 选择或删除所选配置；删除活动配置会清空活动选择。只改配置，不取消在途调用。 |
| is_legacy／migrate_legacy(credential_mode=...) | 旧格式先阻止正常读写；GUI明确确认后才迁移。先保存凭据，再原子替换配置并移除明文；失败保留原文件并尝试撤销本次新建凭据，不另建明文备份。 |
| CredentialStore.put／get | 使用系统凭据库或显式选择的仅进程保存；缺失／不可用明确报错，不回退为无密钥请求或明文文件。 |

系统后端由keyring访问，仅接受原生Windows、macOS、SecretService或KWallet后端，不接受自动挑选的明文文件或混合回退后端。当前测试使用显式注入的假后端，未读写用户实际系统凭据库；真实设备可用性不冒充已经实测。[keyring官方文档](https://keyring.readthedocs.io/en/latest/)

仅进程凭据存在CredentialStore内存中，关闭或切换到其他进程后不可用；配置文件保留的session引用不会恢复密钥。单独启动GUI后再运行NPC，需选系统凭据库，或本地服务不使用认证。旧ModelProfile(api_key=...)构造调用仅兼容为当前进程引用，不再把密钥放入快照字段；新代码推荐通过save输入密钥。

为保持已冻结调用及其后续修正可使用原凭据，编辑／删除配置暂不删除历史凭据；GUI明确说明此行为，历史凭据清理由后续独立维护处理。本检查点没有清理任何已有用户凭据。

## 流式调用契约（2026-10-12已确认）

### 调用与请求

| 接口 | 输入 | 返回与作用 |
| --- | --- | --- |
| `active_profile()` | 无 | 当前活动配置的不可变快照；调用方决定快照复用范围。 |
| `start(messages, profile, connect_timeout_s, read_timeout_s)` | 消息、固定配置及两个正数秒值 | 立即返回本地调用句柄，随后执行网络请求。 |
| `handle.events()` | 无 | 异步接收该调用的正文、结束或故障事件；等待网络不阻塞调用方处理其他工作。 |
| `handle.cancel()` | 无 | 请求停止本地接收并释放连接；实际停止结果由终止事件反馈。重复请求不产生第二次取消操作。 |
| `handle.terminal()` | 无 | 2026-10-16增加的非阻塞只读终态副本；未终止返回None，不消费或重放事件。用于取消后登记实际结果，不增加第二个事件消费者。 |
| `start(..., capture_timing=True)`／`handle.text_timing()` | 显式开启，默认关闭 | 2026-10-17增加可选正文SSE接收时刻及字符区间；getter返回副本，关闭时为None。只作本地观测，不改变原事件或采集独立思考；不等于服务端生成／计算耗时。 |

句柄保存唯一非空字符串`call_id`。事件由所属句柄关联，不重复携带调用标识或NPC轮次／区域字段。NPC适配器在自身记录中关联`call_id`与`attempt_id`。

消息为`role: string`及`content: 非空字符串`；消息列表非空并保持调用方顺序。M1使用一条system消息和一条user消息；通用模块不修改提示词。配置快照沿用已确认的服务地址、模型、凭据引用及可选参数，凭据由模块内部读取，不进入NPC运行快照。

连接超时与读取空闲超时独立。调用方按自身剩余时间传入等待参数，并在整体截止时请求取消；收到网络数据不能延长调用方的整体截止。模块不认识认知轮或1秒／3秒性能评级。

### 事件

| 事件 | 必需字段 | 条件字段 |
| --- | --- | --- |
| 正文增量 | `type="text_delta"`、`text: 非空字符串` | 无；只交付新增正文，不混入独立推理字段。 |
| 调用结束 | `type="end"`、`reason: normal / truncated / refused / cancelled` | 服务提供时保留`raw_reason: string`；本地取消不伪造服务结束原因。 |
| 调用故障 | `type="fault"`、`code: network / timeout / http / protocol`、`message: string` | HTTP故障可附`http_status: int`；错误信息须去除凭据。 |

服务提供用量时，可在终止事件附带`usage`，记录实际可得的输入／输出／总token非负整数，未提供的项省略；用量不是交付门槛，不为等待用量延迟业务处理。

- 每次调用只有一个终止事件：`end`或`fault`，之后不再交付正文。
- 连接断开且没有可靠结束标记，不能判断为正常完成。
- 取消先完成时返回`end(cancelled)`；调用已终止时保留原终止结果，不增加第二个结束事件。
- `cancelled`只表示本地接收已停止，不承诺远端生成停止。
- 模块不自动重试、切换模型或识别区域JSON。通用流处理使用受控HTTP验证；具体应用需独立验证自己的真实服务与业务。

### 当前服务映射与适用限制（2026-10-15）

- 基于Chat Completions的HTTP SSE：请求stream=true、stream_options.include_usage=true；仅支持单个纯文本分支。服务若不兼容该参数，显式返回HTTP故障，不自动重试或换协议。
- delta.content只产生新增正文；空内容、角色及独立推理字段不混入正文。delta.refusal非空或finish_reason=content_filter标记明确拒绝；stop映射normal，length映射truncated。工具调用或未知结束原因不是正常文本成功，记protocol故障。
- 保留有效finish_reason并继续接收已启用的尾部用量；[DONE]或连接正常结束后形成终止事件。仅[DONE]而没有有效finish_reason、未结束断流或畸形事件均不能记正常。正文仍即时交付，NPC业务不等待尾部用量才开始处理。
- usage沿用prompt_tokens／completion_tokens／total_tokens，只有实际提供的非负整数才保存，不补零。当前事件为字典，省略未提供的可选字段。
- start在当前asyncio事件循环返回句柄；events仅允许一个消费者。cancel可重复请求，等待本地连接释放；调用已终止则保留原结果。提前停止消费须显式cancel或关闭异步生成器，不能丢弃仍活动的句柄。
- 连接与读取空闲超时分开；写入／连接池等待采用连接预算。不设整个调用的总截止，不使用NPC轮次或性能评级。当前不读取环境代理、不跟随重定向转发认证；需要代理的服务须后续显式配置，不能推定已支持。
- 异常仅输出固定的分类说明及HTTP状态，不复制响应错误体、URL或底层异常字符串。请求时内部解析凭据引用；凭据不可用则在发送HTTP前报告protocol故障，不静默发送无认证请求。本轮安装验证不执行真实模型联调，不读取用户现有模型密钥。

映射依据：[官方流式事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)和[创建调用](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。这是当前兼容协议范围，不声称所有服务商或模型都已验证。

## 旧同步接口的兼容范围

以下只说明保留的旧generate接口，不作为新版NPC运行入口；旧明文配置必须显式迁移后使用。配置字段、安全保存和快照规则以上节为准，NPC固定配置和切换时机由NPC适配设计管理。

## 责任与调用

`ProfileStore` 保存多组 `ModelProfile` 并选择活动配置；`ModelService.active_profile()` 返回该配置的不可变快照；`ModelService.generate(messages, profile=快照)` 发出一次非流式请求，返回 `ChatResult(content, model, response_id)`。调用方负责构造模型可见消息、解析业务结构、校验来源和决定状态提交。

公共Python导出包括ModelProfile、ProfileStore、CredentialStore、CredentialError、ModelService、ChatMessage、ChatResult、ModelServiceError和StreamHandle，命令行为model-service-config。配置文件的磁盘布局属于本包内部格式，旧布局需显式迁移；包按自身SemVer管理，不跟随NPC子版本递增。

旧generate仍按json_mode决定是否发送response_format，使用timeout_seconds作为同步等待上限；配置创建后不会因活动选择改变而变化。新start不采用旧整份JSON模式，省略参数时采用配置中的两个独立超时。

请求使用 HTTP `POST {base_url}/chat/completions`，发送 `model`、`messages`、`stream: false`，用 Bearer Token 传 API Key。成功结果须包含非空 `choices[0].message.content`；HTTP、连接／超时及无效响应分别返回不含凭据的 `ModelServiceError.code`。不自动重试；调用方决定故障如何进入自身流程。

本包不定义提示词、不保证回复为JSON，也不解释NPC任务。旧同步调用同样通过模块内部解析凭据引用，普通配置不保存API Key；凭据缺失时返回credentials_unavailable，不发送无认证请求。
