# 教务系统课表监控程序

自动登录教务系统，监控课表变化，通过钉钉机器人发送提醒。

- **手动运行**：随时执行一次检查
- **定时运行**：完全由外部 **cron-job** 服务触发（不使用 GitHub Actions 自带定时器）
- **智能提醒**：
  - ✅ 首次运行成功 → 发送启动成功通知
  - 🔔 课表发生变化 → 发送课表变更通知
  - ❌ 运行发生故障/报错 → 发送包含错误日志的报警通知
  - 😶 课表无变化 → 静默不发送任何消息

---

## 📋 目录

- [环境变量配置](#环境变量配置)
- [本地手动运行](#本地手动运行)
- [GitHub 部署 + 外部 cron-job 触发](#github-部署--外部-cron-job-触发)
- [命令行参数](#命令行参数)
- [工作原理](#工作原理)

---

## 🔑 环境变量配置

所有配置均通过**环境变量**传入，避免硬编码敏感信息。

| 变量名 | 必需 | 说明 | 示例 |
|--------|------|------|------|
| `STU_USERNAME` | ✅ | 教务系统账号 | `252310260` |
| `STU_PASSWORD` | ✅ | 教务系统密码 | `15943476796xcaX.` |
| `STU_BASE_URL` | ❌ | 教务系统地址（默认已配置） | `https://stu2.changdian2001.com` |
| `DINGTALK_WEBHOOK` | ✅ | 钉钉机器人 Webhook 地址 | `https://oapi.dingtalk.com/robot/send?access_token=xxx` |
| `DINGTALK_SECRET` | ❌ | 钉钉机器人加签密钥（选"加签"安全时配置） | `SECxxxxxx` |
| `DINGTALK_AT_MOBILES` | ❌ | @指定人手机号，逗号分隔 | `13800138000,13900139000` |
| `DINGTALK_AT_ALL` | ❌ | 是否@所有人（true/false） | `false` |
| `LAST_HASH_FILE` | ❌ | 状态哈希保存路径 | `./last_hash.txt` |
| `LAST_SCHEDULE_FILE` | ❌ | 课表数据保存路径 | `./last_schedule.json` |
| `LOG_LEVEL` | ❌ | 日志级别（DEBUG/INFO/WARNING/ERROR） | `INFO` |

---

## 💻 本地手动运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 设置环境变量

**Windows (PowerShell):**
```powershell
$env:STU_USERNAME = "252310260"
$env:STU_PASSWORD = "15943476796xcaX."
$env:DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=你的token"
```

**Linux / macOS:**
```bash
export STU_USERNAME="252310260"
export STU_PASSWORD="15943476796xcaX."
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=你的token"
```

### 3. 运行程序

```bash
# 正常执行一次检查
python schedule_monitor.py

# 查看当前环境变量配置
python schedule_monitor.py --env

# 发送钉钉测试消息
python schedule_monitor.py --test

# 强制重新检查（清空历史基线）
python schedule_monitor.py --now
```

### 4. 本地定时运行（系统 cron）

如果部署在自己的服务器上，使用系统 crontab：

```bash
# 编辑 crontab
crontab -e

# 每10分钟运行一次（替换为你的实际路径）
*/10 * * * * cd /path/to/project && STU_USERNAME=xxx STU_PASSWORD=xxx DINGTALK_WEBHOOK=xxx /usr/bin/python3 schedule_monitor.py >> monitor.log 2>&1
```

---

## 🚀 GitHub 部署 + 外部 cron-job 触发

### 方案说明

> **完全不使用 GitHub Actions 的 `schedule` 定时器**，改用外部 cron-job 服务通过 API 触发 GitHub Actions 工作流。
>
> 推荐免费 cron-job 服务：[cron-job.org](https://cron-job.org)、[EasyCron](https://www.easycron.com)、[UptimeRobot](https://uptimerobot.com) 等

### 步骤 1：上传代码到 GitHub

将本项目所有文件推送到你的 GitHub 仓库。

### 步骤 2：配置 Secrets

在 GitHub 仓库页面：

1. 进入 **Settings** → **Secrets and variables** → **Actions**
2. 点击 **New repository secret**，依次添加以下 Secrets：

| Secret 名称 | 值 |
|-------------|-----|
| `STU_USERNAME` | `252310260` |
| `STU_PASSWORD` | `15943476796xcaX.` |
| `DINGTALK_WEBHOOK` | 你的钉钉机器人 Webhook 完整地址 |

（可选，按需添加）：
| Secret 名称 | 说明 |
|-------------|------|
| `STU_BASE_URL` | 教务系统地址（默认已内置，一般不需要） |
| `DINGTALK_SECRET` | 钉钉加签密钥 |
| `DINGTALK_AT_MOBILES` | @指定人手机号 |
| `DINGTALK_AT_ALL` | 是否@所有人 |

### 步骤 3：获取 GitHub Personal Access Token

外部 cron-job 需要通过 GitHub API 触发工作流，需要一个 Personal Access Token (PAT)：

1. 进入 GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**
2. 点击 **Generate new token**
3. 配置：
   - 名称：随意（如 `schedule-monitor-cron`）
   - 过期时间：选较长的
   - Repository access：选择 **Only select repositories**，选中你的仓库
   - Permissions → Repository permissions → **Actions** → 设置为 **Read and write**
4. 生成后**复制保存** Token（只显示一次），格式类似 `github_pat_xxxxxxxxxxxxxxxxxxxx`

### 步骤 4：配置外部 cron-job 服务

以 [cron-job.org](https://cron-job.org)（免费）为例：

1. 注册并登录 cron-job.org
2. 点击 **Create cronjob**
3. 填写配置：
   - **Title**: `Schedule Monitor`
   - **URL**: （下面有说明如何构造）
   - **Schedule**: Every 10 minutes
   - **HTTP Method**: `POST`
   - **Headers**: 添加：
     - `Accept`: `application/vnd.github+json`
     - `Authorization`: `Bearer 你的GitHub_PAT_Token`
     - `X-GitHub-Api-Version`: `2022-11-28`
   - **Body (JSON)**:
     ```json
     {
       "ref": "main",
       "inputs": {
         "reason": "scheduled"
       }
     }
     ```

**URL 构造方式：**
```
https://api.github.com/repos/你的用户名/你的仓库名/actions/workflows/monitor.yml/dispatches
```

示例：
```
https://api.github.com/repos/zhangsan/schedule-monitor/actions/workflows/monitor.yml/dispatches
```

4. 点击保存，cron-job 会每10分钟调用一次 GitHub API，触发工作流运行

### 步骤 5：验证运行

1. 进入你的 GitHub 仓库 → **Actions**
2. 应该能看到 "Schedule Monitor" 工作流正在运行（或在 cron-job 面板点击立即执行一次测试）
3. 首次运行成功会发送「启动成功」钉钉通知，同时初始化课表基线
4. 后续每10分钟检查一次：
   - 课表无变化 → 静默不发消息
   - 课表有变化 → 发送「课表变更」通知
   - 登录失败/网络错误/程序异常 → 发送包含错误日志的「运行故障」报警

---

## 📝 命令行参数

| 参数 | 说明 |
|------|------|
| （无参数） | 正常执行一次检查，对比课表变化 |
| `--env` | 显示当前环境变量配置（敏感信息打码） |
| `--test` | 仅发送一条钉钉测试消息，不登录教务系统 |
| `--now` | 强制重新检查（清空历史基线） |

---

## 🔧 工作原理

```
┌─────────────────┐     API 触发      ┌──────────────────┐
│  外部 cron-job   │ ────────────────→ │  GitHub Actions  │
│  (每10分钟)      │                   │  运行 Python 脚本 │
└─────────────────┘                   └────────┬─────────┘
                                                │
                                               执行
                                                │
                             ┌──────────────────┼──────────────────┐
                             │                  │                  │
                             ▼                  ▼                  ▼
                     ┌─────────────┐  ┌────────────────┐  ┌─────────────┐
                     │ 全局异常捕获 │  │  登录教务系统   │  │ 获取并解析课表│
                     └──────┬──────┘  └────────┬───────┘  └──────┬──────┘
                            │                  │                  │
                            │  异常/失败       │ 登录失败         │ 获取失败
                            ▼                  ▼                  ▼
                     ┌─────────────────────────────────────────────────┐
                     │           发送【运行故障】钉钉通知              │
                     │           （附带最近日志 + 错误堆栈）            │
                     └─────────────────────────────────────────────────┘
                                               │
                                               ▼
                                    ┌────────────────────┐
                                    │  对比历史课表哈希  │
                                    └─────────┬──────────┘
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                   首次运行              课表哈希相同           课表哈希不同
                        │                     │                     │
                        ▼                     ▼                     ▼
               ┌────────────────┐      什么都不做             ┌────────────────┐
               │发送【启动成功】 │      静默退出              │发送【课表变更】 │
               │通知+保存基线   │                            │通知+更新状态   │
               └────────────────┘                            └────────────────┘
```

### 通知类型说明

| 通知场景 | 消息标题 | 发送时机 |
|---------|---------|---------|
| 首次启动成功 | ✅【课表监控启动成功】 | 程序第一次运行成功获取课表后 |
| 课表发生变化 | 🔔【教务系统课表变更提醒】 | 检测到课程安排/教师/教室/时间变动时 |
| 运行发生故障 | ❌【课表监控运行故障】 | 登录失败、网络错误、环境变量缺失、程序崩溃等任何异常情况，附带最近运行日志和错误堆栈 |

1. **登录**：通过教务系统加密API登录，获取会话
2. **获取课表**：调用课表查询接口获取当前周课表
3. **哈希对比**：提取课程名/教师/教室/时间核心字段，计算MD5哈希
4. **全局异常保护**：任何步骤发生异常都会被捕获并发送故障通知，不会静默失败
5. **状态持久化**：状态文件通过 git commit 到仓库，实现持久化

---

## ⚠️ 注意事项

- 钉钉机器人安全设置建议使用**自定义关键词**，关键词填「课表」即可
- GitHub Actions 每次运行约需 30-60 秒，cron-job 设置 10 分钟间隔足够
- 首次运行不发消息，仅保存当前课表作为基线
- 如果误发或需要重新建立基线，删除 `last_hash.txt` 和 `last_schedule.json` 即可
