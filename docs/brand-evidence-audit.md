# 企业知识与小程序截图证据核查

本轮只核查已有资料和实际截图，不创建虚构小程序页面，不用官网截图冒充小程序。项目及祖先目录未找到 `AGENTS.md`。未读取密钥文件、个人订单数据库或业务私密配置。

## 可用资料与边界

已有导入资料为 26 条公开产品知识、10 条前后端实现记录，再加用户明确的小程序名称，共 37 条确认资料。公开知识库文件为 `super-ecar-review/carRental/knowledge/super-ecar-public/dist/knowledge.jsonl`，SHA256 为 `62d786a54622edf2dd6d538f0d6a7215f5aae7152e655a73183ba1651fc54ab8`。这些是保存的产品与本地实现资料，不能自动推断线上发布状态、当前库存、实际审批、履约、签约、保险有效或收益。

| 稳定事实 ID | 来源 | 可用表达与限制 | 可用画面 |
| --- | --- | --- | --- |
| `public-hosting-assistant-entry` | 知识库第 25 行；`pages/carpark/carpark.vue:96–114` 与 `:191–201` | 托管页提供“智能托管”汽车问答助理入口，“新增托管”为独立申请入口。静态源码不证明线上可用。 | 已核对的汽车助理首页；证明页面和提问入口，不演示跳转成功。 |
| `public-hosting-materials` | 知识库第 18 行；`pagesC/carpark/addHosting.vue:220–242`、`:717–739` | 申请需要车辆照片、行驶证、年检与保险材料等，应在正式表单提交。不能说已经审核通过。 | 当前没有申请表单实拍图；不能把汽车助理首页说成已提交材料的画面。 |
| `verified-source-hosting-application` | `pagesC/carpark/addHosting.vue:722`；`uniCloud-aliyun/cloudfunctions/submitHosting/index.js:316`、`:501` | 前后端有申请必填检查，新申请进入待审核。不能证明证件有效或必定通过。 | 可用现场素材做情境补充；没有该表单真实截图。 |
| `public-hosting-share-policy` | 知识库第 24 行；条目记录的管理端审核配置来源 | 分成并非所有车主统一固定数值，具体安排以确认协议为准；现有资料不提供保底收益承诺。 | 不生成收益数字或假账单；没有相应管理端实拍图。 |
| `verified-source-handover-records` | `pagesA/order/orderInfo.vue:416`、`:632`；`uniCloud-aliyun/cloudfunctions/orderInfo/index.js:236`、`:318` | 本地实现保存交还车的里程、照片、视频等记录。资料留存不等于自动车损鉴定或保证无争议。 | 现有真实现场视频仅作情境，不冒充某笔订单记录。 |
| `public-smart-selection-entry` | 知识库第 13 行及条目列明的本地界面来源 | 描述用车需求后主动点击匹配，匹配不是自动下单。 | 当前没有智能选车实拍页；静态图标不算完整界面。 |

逐条本地文件存在性与 SHA256 比对报告保存在本次工作目录 `work/brand-production-evidence-20261104/brand-source-audit.json`，详细原始资料在 `work/brand-import-20261104`。源码有改动的条目不能只凭旧行号认证，需要重新核对其明确范围。

## 已查看的真实图片

- 已有资产 ID：`a9406be6f24659539195dccb6156a26c`，364×705 PNG。
- 原已有名称：Super Ecar 智能托管问答首页（本地实拍界面）。
- 已下载核查的副本：`work/brand-production-evidence-20261104/miniapp-hosting-existing.png`。
- 实际图像 SHA256：`f2d27e69a7654a6ae8bfc6142a804f4f14a1f7aecb4c636e63891af61b971ed3`。
- 可见“汽车助理”“联网开启”“托管申请”等分类和问题入口，与 `pagesC/carpark/smartHosting.vue:4–18` 一致；源文件 SHA256 为 `c7ef4afb47d708833d2b7a5aadf674a2ac9f848d5d02edf958619a1a1c62dc5d`。
- 仅绑定 `public-hosting-assistant-entry`。图像不证明联网回答正确、正式小程序已发布、申请已提交、合同已签署或收入已发生。

已准备更新已有图片元数据的 JSON：`work/brand-production-evidence-20261104/bind-existing-miniapp-screenshot.json`。部署后向 `PUT /api/brand-context/screenshots/a9406be6f24659539195dccb6156a26c/evidence` 提交；不重新上传、替换或改画面。是否已在运行服务执行，须以实际更新返回与后续制作记录为准。

## 品牌模块接口

`brand_context.model_context(data, query=...)` 保留每条确认事实的稳定 `id`、`source`、`status`，增加 `fact_sources` 与 `brand_assets`。明确要求小程序画面的目标会提高已确认截图关联事实的召回优先级，仍受最多 8 条与原文本预算限制，不改事实内容。

`brand_media.production_assets(data, fact_ids=None)` 只返回已核对来源、已关联当前确认事实且实际 PNG 与哈希一致的小程序图片。结果包含 `id/name/kind/capture_mode/page_route/source_reference/source_sha256/fact_ids/purpose/verification_scope/sha256/width/height/url`，不给模型本机路径。网站截图、未审阅旧上传、失效事实关联、被改动的图片不会进入自动制作资产。

`resolve_screenshot(data, screenshot_id, expected_sha256=None)` 验证文件边界、存储哈希和可选制作版本哈希；不一致时明确失败，不能静默换图。`bind_screenshot(..., expected_sha256=...)` 要求调用方核对当前图片后提供其哈希，绑定不会将待核实事实升级为确认事实。

截图只提供背景说明或界面展示；是否真正插入成片、时长和缩放是否清晰，必须由后续渲染与实际成片检查证明。本次模块测试不能替代这一步。
