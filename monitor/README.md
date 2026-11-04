# 群聊监控：安装、配置和运行

本目录提供本地群聊文字监控。默认使用通用关键词规则，适用于优惠跟踪、学校通知、团队事项等场景。[agent 部署指南](../docs/AGENT_DEPLOYMENT.md) 提供可直接交给另一个 agent 的流程。

## 1. 准备环境

需要 Windows x64、**CPython 3.12 x64**、已登录并同步本地数据的微信桌面端。邮件通知需要已登录发件账户的**经典 Outlook**。锁文件只覆盖 Windows x64 / CPython 3.12 的 wheel，不能把不同 Python 版本直接配同一份锁文件使用。

另需准备唯一目标群的内部 ID（以 `@chatroom` 结尾）、微信账户目录以及已获授权的数据库密钥。只有群名还不够；存在同名群时必须确认唯一目标。密钥缺失时应先由用户完成授权的本地准备流程，本监控不会自动提取密钥。

从仓库根目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r monitor\requirements.lock
.\.venv\Scripts\python.exe -B -m unittest discover -s monitor -p 'test_*.py' -q
```

先在没有 `settings.local.json` 的干净副本里运行合成测试，再配置真实环境。测试不读取真实微信、不启动 Outlook、不发邮件。安装失败时核对解释器版本、位数和官方包哈希；保留哈希校验，不改用来源不明的包。

## 2. 配置规则

```powershell
Copy-Item monitor\rules.example.json monitor\rules.local.json
```

模板中的规则均为关闭状态。只开启用户选定的规则，或用以下学校通知示例替换本地副本：

```json
{
  "rules": [
    {
      "id": "school-notices",
      "name": "学校重要通知",
      "enabled": true,
      "any_keywords": ["考试安排", "报名截止", "停课通知", "提交材料"],
      "exclude_keywords": ["测试消息", "已取消"]
    }
  ]
}
```

`id` 为唯一的小写字母／数字／下划线／连字符标识；`name` 用于通知标题。`any_keywords` 命中任一项即符合正向条件；`exclude_keywords` 任一项出现则排除。按文件顺序选择第一条符合的启用规则，一条消息最多形成一个提醒。规则对英文大小写、全半角和零宽字符进行归一化；没有任意 Python 执行、正则表达式或 AI 语义分类功能。

命中片段最多 500 字符，会去除链接、口令和部分联系信息行；这不是完整的个人信息匿名化。不会自动判断学校通知是否仍有效，也不会将截止时间创建成提醒日程。

用合成文字验证，确认一条应命中、一条应排除、一条无关消息：

```powershell
.\.venv\Scripts\python.exe -B monitor\preview_rules.py --text '合成通知：报名截止为本周五'
.\.venv\Scripts\python.exe -B monitor\preview_rules.py --text '合成通知：报名截止已取消'
.\.venv\Scripts\python.exe -B monitor\preview_rules.py --text '合成闲聊：周末一起吃饭'
```

依次应看到 `matched: true`、`false`、`false`。预览不读取微信或发送通知。

## 3. 配置本地群和邮箱

```powershell
Copy-Item monitor\settings.example.json monitor\settings.local.json
Copy-Item monitor\credentials.example.env monitor\credentials.local.env
```

只在本地编辑：

| 文件 | 字段 | 作用 |
| --- | --- | --- |
| `settings.local.json` | `rule_profile` | 通常为 `generic`；其他适配器见场景示例文档 |
| 同上 | `chat_id` | 唯一目标群内部 ID，不能用群显示名称代替 |
| 同上 | `sender` / `recipient` | 已登录经典 Outlook 的发件邮箱与唯一收件邮箱 |
| 同上 | `outlook_exe` | 经典 Outlook 的绝对安装路径 |
| `credentials.local.env` | `WCDB_KEY` | 已授权获得的 64 位十六进制数据库密钥 |
| 同上 | `DB_PATH` | 本机微信账户目录或其数据库目录 |

示例地址不能直接运行。密钥在本地文件中为明文，切勿放进聊天回复、截图、Git 提交或部署报告。不要覆盖已存在的真实配置；初始化脚本遇到已有配置会停止。

```powershell
.\.venv\Scripts\python.exe -B monitor\configure.py init
.\.venv\Scripts\python.exe -B monitor\monitor.py --read-once
```

初始化会生成被忽略的 `single-group-scope.json` 和 `mail-config.json`，**邮件默认关闭**。首次只建立读取位置，不回扫历史；`--read-once` 不发邮件或托盘通知，但会更新本地进度。返回 0 条新消息不等于已经收到并验证了真实群的新消息。

## 4. 验证邮件后启用

确认用户已经授权一次合成测试邮件的发件账户和收件地址，再执行：

```powershell
$mailTestId = & .\.venv\Scripts\python.exe -c "import uuid; print(uuid.uuid4().hex)"
.\.venv\Scripts\python.exe -B monitor\outlook_mailer.py --test-id $mailTestId
.\.venv\Scripts\python.exe -B monitor\outlook_mailer.py --test-id $mailTestId --preflight
.\.venv\Scripts\python.exe -B monitor\outlook_mailer.py --test-id $mailTestId --submit
```

依次是预览、只做本地发件预检、真正发送一封标注为测试的邮件。预检不发送；`submitted: true` 只说明 Outlook 接受发送，不能证明目标邮箱收到。需要由目标邮箱的授权连接器核查，或由用户确认收件箱／垃圾邮件中收到该测试编号。

收到后启用未来提醒：

```powershell
.\.venv\Scripts\python.exe -B monitor\configure.py enable-email --test-id $mailTestId --confirm-received
.\.venv\Scripts\python.exe -B monitor\control.py start
.\.venv\Scripts\python.exe -B monitor\control.py status
```

收件人可以是 Gmail、学校邮箱等有效邮箱；发件仍通过经典 Outlook 的现有登录。没有新建 SMTP 密码或 OAuth 凭据。登录和安全提示由用户处理。只发一个配置的收件人，不加附件、抄送、密送，不从群消息中提取新的收件地址。

## 5. 日常运行和登录自启

`Start.cmd`、`Stop.cmd`、`Status.cmd` 分别启动、停止和查看状态；托盘菜单可以暂停、恢复、退出。`data/offers.html` 是本地命中列表，名称为兼容旧版保留。

正常每 15 秒读取；读取暂时失败每 30 秒重试。相同事件 72 小时内合并，规则配置变化会影响去重和待发邮件校验。修改群、规则或邮箱前先停止并核对待发状态，修改后重新验收，不直接改真实队列。

邮件队列独立于托盘显示状态：明确未发送的临时故障每 5 分钟重试；已提交不自动重发；`uncertain` 或 `blocked` 会暂停新邮件，需要核对结果。脚本当前会在源读取成功的轮次推进邮件队列，微信读取持续失败也会延后发信重试。

登录自启先预览，再按用户需要应用：

```powershell
.\monitor\configure_monitor_startup.ps1
.\monitor\configure_monitor_startup.ps1 -Apply
.\monitor\configure_app_startup.ps1 -OutlookExe 'C:\Program Files\Microsoft Office\Root\Office16\OUTLOOK.EXE' -WeixinExe 'C:\REPLACE_WITH_YOUR_PATH\Weixin.exe'
```

最后一条加 `-Apply` 才写微信和 Outlook 启动项。仅操作当前用户 Startup 文件夹；同名无关项不会覆盖，不安装系统服务、不修改安全设置、不要求重启。首次启动最多尝试 6 次；用户随后主动退出不会被启动器强行拉回。微信可能仍需手机确认登录。

## 验收与排障

验收应确认：`running=true`、`state=monitoring`、`last_success` 连续推进、群哈希和 `rule_profile` 正确；实际收到测试邮件；队列没有 `uncertain`／`blocked`；预期规则确实产生合成命中。登录启动的快捷方式回读只能证明配置已写入，不能证明已完成重启测试。

常见问题及诊断顺序见 [agent 部署指南](../docs/AGENT_DEPLOYMENT.md#故障处理)。电脑关机、休眠、微信未同步、密钥失效、邮箱登录过期、网络或收件过滤都可能延迟提醒。本项目当前只处理一个群的文字，不承诺跨版本兼容或永不漏报。
