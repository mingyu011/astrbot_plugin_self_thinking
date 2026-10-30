# 自主思维（astrbot_plugin_self_thinking）

**当前版本：1.2.0**

让主 Agent 自己决定要不要主动开口，也给它一小段自己的时间。

这个插件不生成、不代发主动消息。它负责：

1. 在合适的时间给主 Agent 一次“主动开口机会”；
2. 提供 `send_emoji` 工具，让模型自己决定要不要配一张本地表情包；
3. 在专用工作区里自主看屏幕、写东西、只读浏览网页；高风险动作必须先请求确认。

真正说不说、说什么，由 AstrBot 主 Agent 决定；它可以选择沉默。

### 可联动插件

先用“智能图片对话”给表情库生成标签，再启用本插件；装了“全量记忆体”时，主动开口前会自动取一份记忆摘要。

## 为什么不用直接发消息

直接 `context.send_message` 的主动消息：

- 不经过 AstrBot 的 respond 阶段；
- 不经过 `on_agent_done`；
- 不进正常会话历史；
- all_memory 看不到，下一轮主模型会以为“我没发过”。

所以这里改用 AstrBot 的 `ActiveAgentCronJob` 唤醒主 Agent，由模型自己调用 `send_message_to_user`。主动消息会进入正常历史，下一轮它自己知道。

## 安装

把整个 `astrbot_plugin_self_thinking` 文件夹放进 AstrBot 的插件目录，重启或重载。

需要 AstrBot 4.29 及以上（用到 `ActiveAgentCronJob` 唤醒主 Agent）。

插件配置里可以调：

- 是否开启；
- 检查间隔；
- 至少安静多少分钟；
- 两次主动机会的最小间隔；
- 每个私聊窗口每天最多给几次机会；
- 安静时段；
- 记忆摘要上限；
- 是否允许发表情包；
- 是否允许自主行动、看屏幕、写工作区、读网页；
- 是否允许用现有浏览器打开网页、读取历史；
- 看图 Provider。

### 这些空白怎么填

先记住一个原则：**能留空的就留空，插件会自动找。** 只有自动找错了才手填。

| 配置项 | 留空时 | 要手填时填什么 |
|---|---|---|
| 专用工作目录 `workspace_dir` | 插件数据目录下的 `workspace` | 在电脑上新建一个文件夹，进入后复制资源管理器地址栏路径，例如 `D:\AI\self_thinking_workspace`。填文件夹，不是文件。 |
| 表情库数据目录 `image_plugin_data_dir` | 自动找智能图片对话的数据目录 | 打开 `AstrBot/data/plugin_data/astrbot_plugin_smart_imagechat_hub`，复制地址栏完整路径。 |
| 表情索引文件 `image_index_path` | 自动找上面的 `image_index.json` | 指向上面的 `image_index.json` 文件，不是文件夹。 |
| 智能图片配置 `image_config_path` | 自动找 `AstrBot/data/config/astrbot_plugin_smart_imagechat_hub_config.json` | 指向这个 `.json` 文件。 |
| 看图模型 `screen_caption_provider_id` | 用当前聊天模型；DeepSeek v4.1 flash 支持看图 | 配置页会显示 AstrBot 已配置模型的下拉框，直接选就行，不用手填 ID。 |
| 浏览器历史 `browser_history_paths` | 自动找 Chrome / Edge / Firefox | 填 `History` 或 `places.sqlite` 文件的完整路径，不是浏览器安装目录；多个用分号隔开。 |

“从电脑中选”目前没有目录选择按钮；最稳的做法就是复制资源管理器地址栏的路径粘贴进去。

## 使用

**不需要绑定窗口。** 插件启用后自动适用于所有私聊窗口：

- 任何私聊第一次说话，都会自动加入窗口列表；
- 每个窗口单独计算活跃时间、主动间隔和每日次数；
- 新窗口不需要额外设置，发过消息就会自动生效。

常用指令（中英文都可用）：

```text
/自主思维              查看状态（会列出最近几个窗口）
/自主思维 now          给当前私聊窗口一次主动开口机会
/自主思维 on           开启
/自主思维 off          只关闭主动开口机会
/自主思维 暂停          一键暂停所有自主行动
/自主思维 恢复          恢复自主行动
/自主思维 待确认        查看等待主人同意的请求
/自主思维 日志          查看最近审计
/自主思维 emoji 开心    测试发表情包
/自主思维 reload       刷新表情库
```

`/自主思维 now` 只是“给机会”，主 Agent 仍然可以判断后不说话。`here` 已经不需要了；旧习惯发了也只会提示“无需绑定”。

`/selfthink` 是 `/自主思维` 的英文别名，功能完全一样。

## 自主行动

开启 `autonomy_enabled` 后，主动唤醒时模型可以自己决定做点事，不是每次都必须发消息。

可用工具：

- `self_look_screen`：只读截屏，保存到工作区，并尝试用看图模型描述画面。
- `self_write_note` / `self_read_note` / `self_list_notes`：只在专用工作区读写。
- `self_browser_history`：读取本机 Chrome/Edge/Firefox 最近历史，使用现有账户数据，不读 Cookie/密码。
- `self_open_url`：用默认浏览器打开网页，沿用你现有的登录状态，不新开独立 profile、不退出登录。
- `self_browse_web`：只读 HTTP 读取公开网页正文，不登录、不带 Cookie、不提交表单、不下单、不支付。

### 专用环境

- 建议把 AstrBot 放在**专用 Windows 用户或虚拟机**里，不要用日常主机的管理员账号。
- 专用工作区默认在 `data/plugin_data/astrbot_plugin_self_thinking/workspace/`，可用 `workspace_dir` 改。
- 浏览器打开沿用你原本的浏览器登录状态；不要给它单独做一个退出登录的 profile。
- 历史读取只读 History/places 数据库；不读 Cookie、密码和支付信息。
- 截屏保存的是本地 PNG。`screen_caption_enabled` 开启且 Provider 支持图片时，截图会发给该 Provider 做画面描述；不想发就关掉它，只保存本地截图。

### 高风险动作

安装、删除、支付、给第三人发消息，不会由模型直接执行。

不是系统按钮：模型真的想做时，先调用 `self_request_action` 记住自己的想法，然后**用它自己的话**主动给你发消息，说明想做什么、为什么、多少钱/有什么后果。你直接在聊天里自然回复：

```text
可以，买吧
去吧
不要，算了
```

模型看到你的自然回复后，再调用 `self_execute_action`。没有明确同意、你在提问、或者只是普通聊天，它都不会执行。

规则：

- `delete`：模型在请求里说清要删哪个路径，你确认后才执行；不会在未确认时动任何文件。
- `install`：模型说清要装什么、为什么，你确认后才执行 `pip install`。
- `payment`：**永远不会自动付款**；模型可以请求同意并打开购买/付款页面，沿用你的登录，最后一键付款仍由你手动完成。
- `message_third_party`：你自然同意后才通过 AstrBot 发送，目标会话由调用方填写。

### 暂停与审计

- `/自主思维 暂停`：立即停止看屏幕、写工作区、读网页、打开网页和待执行动作；`/自主思维 恢复` 恢复。
- 所有自主动作写入 `audit.jsonl`，包括截屏、网页、历史、写文件、请求、执行、暂停。
- 工作区默认只允许写文件，不允许递归删除目录。


## 表情包

表情库直接读智能图片对话已经打好的标签，不需要智能图片对话保持启用：

```text
data/plugin_data/astrbot_plugin_smart_imagechat_hub/image_index.json
data/plugin_data/astrbot_plugin_smart_imagechat_hub/files/library_builder/image_files/
```

找不到 `image_index.json` 时，会退回到它的配置：

```text
data/config/astrbot_plugin_smart_imagechat_hub_config.json 里的 image_tags
```

模型通过 `send_emoji` 工具调用：

```text
mood：开心 / 有点吃醋 / 晚安 / 无语
tags：得意,害羞,猫
```

插件按标签和当前语境打分，最近用过的图会降权；没有足够匹配就不发。默认不会为了发图而随机发图。

表情是模型先调用工具选中，正常回复的装饰阶段再把选中的图追加到文字末尾；主动唤醒路径则直接发送。工具调用和结果会进入会话历史，图片文件本身是否进入 AstrBot 的文本历史取决于宿主；all_memory 的附件钩子会尽量把它归到上一条助手消息上。

## 和 all_memory 的关系

主动唤醒前，插件会尝试通过 AstrBot 插件注册表找到 `astrbot_plugin_all_memory`，调用它的：

```text
compose_proactive_context()
```

取一份纪要、长期事实和相关原文，塞进主动提示词。

- 没装 all_memory：自动降级，只靠当前会话历史；
- all_memory 刚启动还没就绪：这次不带摘要，不影响主动任务；
- 不直接读取 all_memory 的私有文件，不绕过它的权限。

主动消息由主 Agent 发出后，AstrBot 会持久化这段历史；下一次正常对话时 all_memory 可以从上下文补归档。

## 边界

- 只支持私聊；群聊不会主动开口，也不会发表情。
- 不做“插件替模型写一句话再让模型认账”。
- 不保证每天一定发消息；模型可以一直选择沉默。
- 主动消息仍然会走当前聊天 Provider，会计费。
- 表情文件只从本地库读取，不会上传图片。
- 看屏幕需要 AstrBot 在交互式 Windows 会话里运行；服务/无桌面会话可能截不到。
- 看图需要 Provider 支持图片输入；DeepSeek v4.1 flash 已支持，留空 `screen_caption_provider_id` 就会用当前聊天 Provider。
- 网页读取是只读 HTTP，不登录、不带 Cookie，动态网页可能读不全。
- 支付永远不会自动执行；安装、删除、第三方消息必须确认。

## 版本

### 1.2.0

- 浏览器沿用现有登录：新增 `self_browser_history` 读取本机 Chrome/Edge/Firefox 历史，新增 `self_open_url` 用默认浏览器打开网页，不再做单独退出登录的 profile。
- 高风险动作改成自然语言请求：模型自己说明想做什么、为什么、多少钱，主人自然回复“可以/买吧/不要”，模型再用 `self_execute_action` 执行；不再发固定编号的确认卡。
- 去掉安装开关和删除范围限制：安装、删除、给第三人发消息都先由模型主动和你交流，你确认后才执行；支付确认后只打开付款页，最后一步手动完成。
- 支付仍不会自动扣款：同意后只打开购买/付款页面，最后一步由主人手动完成。
- 明确 DeepSeek v4.1 flash 已支持看图；截图描述可直接用当前 Provider。

### 1.1.0

- 新增自主行动：只读看屏幕、专用工作区写作、只读网页。
- 新增强制确认队列：安装、删除、支付、给第三人发消息必须先请求确认。
- 删除只允许工作区；安装默认只提示，打开开关后只接受 pip 包名；支付永不自动执行。
- 新增审计日志 `audit.jsonl`、一键暂停/恢复。
- 主动提示里加入自主行动规则。

### 1.0.1

- 去掉“先发 `/自主思维 here` 绑定窗口”的步骤。
- 启用后自动适用于所有私聊窗口；每个窗口独立计算活跃时间、主动间隔和每日次数。
- 新窗口第一次说话会自动加入，不再需要手动设置目标。
- 旧版单窗口状态会自动迁移到多窗口列表。

### 1.0.0

- 新增自主思维：定时给主 Agent 主动开口机会，由模型决定是否发送。
- 新增 `send_emoji` 工具：从智能图片对话的标签库选择本地表情，模型决定是否发送。
- 新增私聊窗口记录、安静时段、间隔、每日上限、手动触发和状态指令。
- 新增 all_memory 主动记忆摘要桥接；未安装时自动降级。
