#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
教务系统课表监控程序
功能：登录教务系统，监控课表变化，通过钉钉机器人发送提醒
可手动运行，也可配合外部 cron-job 每隔10分钟自动运行

所有配置均通过环境变量传入，详见 README.md

通知规则：
1. 首次运行成功（初始化基线）-> 发送启动成功通知
2. 课表发生变化 -> 发送课表变更通知
3. 运行发生故障/报错 -> 发送包含错误日志的报警通知
4. 课表无变化 -> 静默不发送消息
"""

import requests
import base64
import urllib.parse
import json
import hashlib
import os
import sys
import logging
import traceback
import io
from datetime import datetime

# ==================== 配置区域（全部从环境变量读取）====================

# 教务系统配置
USERNAME = os.environ.get("STU_USERNAME", "")
PASSWORD = os.environ.get("STU_PASSWORD", "")
BASE_URL = os.environ.get("STU_BASE_URL", "https://stu2.changdian2001.com")

# 钉钉机器人Webhook地址
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")

# 钉钉安全设置（可选）
# 加签密钥（钉钉机器人安全设置选择"加签"时使用）
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", "")
# @指定人手机号（逗号分隔）
DINGTALK_AT_MOBILES = os.environ.get("DINGTALK_AT_MOBILES", "")
# 是否@所有人
DINGTALK_AT_ALL = os.environ.get("DINGTALK_AT_ALL", "false").lower() == "true"

# 状态文件保存路径（用于对比变化）
# 默认在脚本同目录，GitHub部署时可配置为仓库内路径以便git持久化
LAST_SCHEDULE_FILE = os.environ.get(
    "LAST_SCHEDULE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_schedule.json")
)
LAST_HASH_FILE = os.environ.get(
    "LAST_HASH_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_hash.txt")
)

# 日志级别
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# 日志配置
class StringLogHandler(logging.Handler):
    """用于收集日志到内存字符串的处理器"""
    def __init__(self):
        super().__init__()
        self.log_stream = io.StringIO()
        
    def emit(self, record):
        msg = self.format(record)
        self.log_stream.write(msg + '\n')
    
    def get_logs(self):
        return self.log_stream.getvalue()
    
    def clear(self):
        self.log_stream.truncate(0)
        self.log_stream.seek(0)

# 创建内存日志处理器用于故障时发送日志
memory_log_handler = StringLogHandler()
memory_log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        memory_log_handler
    ]
)
logger = logging.getLogger(__name__)
# ======================================================================


def encode_param(data_dict):
    """将参数字典编码为API要求的格式：JSON -> URL编码 -> Base64"""
    json_str = json.dumps(data_dict, ensure_ascii=False)
    url_encoded = urllib.parse.quote(json_str)
    b64_encoded = base64.b64encode(url_encoded.encode('utf-8')).decode('utf-8')
    return b64_encoded


def check_required_env():
    """检查必需的环境变量是否已配置"""
    missing = []
    if not USERNAME:
        missing.append("STU_USERNAME（教务系统账号）")
    if not PASSWORD:
        missing.append("STU_PASSWORD（教务系统密码）")
    if not DINGTALK_WEBHOOK:
        missing.append("DINGTALK_WEBHOOK（钉钉机器人Webhook）")
    
    if missing:
        logger.error("以下必需的环境变量未配置：")
        for m in missing:
            logger.error(f"  - {m}")
        logger.error("请设置后再运行程序，详见 README.md")
        return False
    return True


class ScheduleMonitor:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/json;charset=utf-8',
            'Accept': 'application/json, text/plain, */*',
            'Origin': BASE_URL,
            'Referer': f'{BASE_URL}/Login'
        })

    def login(self):
        """登录教务系统"""
        try:
            # 先访问登录页获取session
            self.session.get(f'{BASE_URL}/Login', timeout=(5, 25))

            # 构造登录请求
            login_data = {
                'Dto': {
                    'LoginName': USERNAME,
                    'PassWord': PASSWORD,
                    'Code': ''
                }
            }
            payload = {
                'param': encode_param(login_data),
                '__log': {'Logtype': 1}
            }

            response = self.session.post(
                f'{BASE_URL}/api/LoginApi/LocalLogin',
                json=payload,
                timeout=(5, 25)
            )
            result = response.json()

            if result.get('state') == 0:
                logger.info("登录成功")
                return True
            else:
                logger.error(f"登录失败: {result.get('message', '未知错误')}")
                return False
        except Exception as e:
            logger.error(f"登录异常: {str(e)}")
            return False

    def check_refresh_button(self):
        """扫描页面状态，检测是否显示刷新按钮
        
        刷新按钮显示条件（与前端JS逻辑一致）：
        当在线选课/重修申请/实验预约任一项处于进行中时，页面会显示刷新按钮
        按钮文字：刷新，图标：el-icon-refresh
        点击后会重新拉取最新课表数据
        """
        try:
            # 调用首页接口获取进行中事项 SJXS
            home_payload = {
                'param': encode_param({}),
                '__permission': {
                    'MenuID': '00000000-0000-0000-0000-000000000000',
                    'Operate': 'select',
                    'Operation': 0
                },
                '__log': {
                    'MenuID': '00000000-0000-0000-0000-000000000000',
                    'Logtype': 6,
                    'Context': '查询'
                }
            }
            response = self.session.post(
                f'{BASE_URL}/api/ClientStudent/Home/StudentHomeApi/GetStudentHome',
                json=home_payload,
                timeout=(5, 25)
            )
            result = response.json()

            if result.get('state') != 0:
                logger.warning(f"获取首页状态失败: {result.get('message', '未知错误')}")
                return False, []

            sjxs = result.get('data', {}).get('SJXS', [])
            running_items = []
            # 与前端逻辑一致：在线选课、重修申请、实验预约 任一进行中即显示刷新按钮
            for item in sjxs:
                menu_name = item.get('MenuName', '')
                is_running = item.get('IsRunning', False)
                if is_running and menu_name in ['在线选课', '重修申请', '实验预约']:
                    start_date = item.get('StartDate', '')
                    end_date = item.get('EndDate', '')
                    running_items.append({
                        'name': menu_name,
                        'start': start_date,
                        'end': end_date
                    })

            refresh_visible = len(running_items) > 0
            if refresh_visible:
                items_str = ', '.join([f"{i['name']}({i['start']}~{i['end']})" for i in running_items])
                logger.info(f"检测到页面显示刷新按钮，进行中事项: {items_str}")
            else:
                logger.info("页面未显示刷新按钮（无正在进行的选课相关事项）")

            return refresh_visible, running_items
        except Exception as e:
            logger.warning(f"检测刷新按钮状态异常: {str(e)}，将跳过自动刷新")
            return False, []

    def trigger_refresh_schedule(self):
        """模拟点击刷新按钮，重新拉取最新课表
        
        对应前端 handleReloadSchedule 方法：
        1. 调用 GetSchoolShortName 获取学期/校区信息
        2. 调用 QueryStudentScheduleData 获取最新课表
        3. 调用 QueryStudentPracticeData 获取最新实践课数据
        """
        try:
            logger.info("正在自动刷新课表数据...")

            # 步骤1：获取学校学期/校区信息
            xqjc = ''
            try:
                r = self.session.post(
                    f'{BASE_URL}/api/PublicQueryApi/GetSchoolShortName',
                    json={'action': 'select', 'data': {}},
                    timeout=(5, 25)
                )
                school_result = r.json()
                school_data = school_result.get('data', {}) or {}
                if school_data.get('SFQYXQJC'):
                    xqjc = school_data.get('MRXQ', '') or (school_data.get('XQJCS') or [''])[0]
                    logger.info(f"启用校区选择，当前校区: {xqjc}")
                else:
                    logger.info("未启用校区选择，使用默认")
            except Exception as e:
                logger.warning(f"获取学校信息失败，使用默认校区: {str(e)}")

            # 步骤2：重新拉取课表数据（这是刷新的核心，相当于强制更新缓存）
            schedule_data = self._fetch_schedule_raw(xqjc)
            if not schedule_data:
                logger.error("刷新后获取课表失败")
                return None

            # 步骤3：拉取实践课数据（前端会一并刷新，这里也调用以确保数据最新）
            try:
                practice_payload = {
                    'param': encode_param({}),
                    '__permission': {
                        'MenuID': '00000000-0000-0000-0000-000000000000',
                        'Operate': 'select',
                        'Operation': 0
                    },
                    '__log': {
                        'MenuID': '00000000-0000-0000-0000-000000000000',
                        'Logtype': 6,
                        'Context': '查询'
                    }
                }
                self.session.post(
                    f'{BASE_URL}/api/ClientStudent/Home/StudentHomeApi/QueryStudentPracticeData',
                    json=practice_payload,
                    timeout=(5, 25)
                )
                logger.info("实践课数据已刷新")
            except Exception as e:
                logger.warning(f"刷新实践课数据失败（不影响主课表）: {str(e)}")

            logger.info("课表数据刷新完成，已获取最新数据")
            return schedule_data
        except Exception as e:
            logger.error(f"自动刷新课表异常: {str(e)}")
            return None

    def _fetch_schedule_raw(self, xqjc=''):
        """底层课表获取（供内部调用）"""
        try:
            schedule_data = {'XQJC': xqjc}
            payload = {
                'param': encode_param(schedule_data),
                '__permission': {
                    'MenuID': '00000000-0000-0000-0000-000000000000',
                    'Operate': 'select',
                    'Operation': 0
                },
                '__log': {
                    'MenuID': '00000000-0000-0000-0000-000000000000',
                    'Logtype': 6,
                    'Context': '查询'
                }
            }

            response = self.session.post(
                f'{BASE_URL}/api/ClientStudent/Home/StudentHomeApi/QueryStudentScheduleData',
                json=payload,
                timeout=(5, 25)
            )
            result = response.json()

            if result.get('state') == 0:
                return result.get('data', {})
            else:
                logger.error(f"获取课表失败: {result.get('message', '未知错误')}")
                return None
        except Exception as e:
            logger.error(f"获取课表异常: {str(e)}")
            return None

    def get_schedule(self):
        """获取当前课表数据
        流程：登录后先检测页面是否有刷新按钮，有则自动刷新课表再获取；无则直接获取
        """
        # 1. 扫描页面，检测是否有刷新按钮
        refresh_visible, running_items = self.check_refresh_button()

        # 2. 如果显示刷新按钮，自动点击刷新获取最新课表
        if refresh_visible:
            schedule_data = self.trigger_refresh_schedule()
            if schedule_data:
                return schedule_data
            logger.warning("自动刷新失败，尝试直接获取课表...")

        # 3. 无刷新按钮或刷新失败，直接获取课表
        return self._fetch_schedule_raw('')

    def parse_schedule(self, data):
        """解析课表数据为易读格式"""
        schedule_text = []
        course_list = []

        adjust_days = data.get('AdjustDays', [])
        week_days = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']

        for day in adjust_days:
            day_name = day.get('FullTitle', '')
            day_courses = []

            for period_key, period_name in [
                ('AM__TimePieces', '上午'),
                ('PM__TimePieces', '下午'),
                ('NT__TimePieces', '晚上')
            ]:
                for piece in day.get(period_key, []):
                    for dto in piece.get('Dtos', []):
                        if dto and dto.get('Content'):
                            course_info = self._parse_course_dto(dto, day_name, period_name)
                            if course_info['course_name']:
                                day_courses.append(course_info)
                                course_list.append(course_info)

            if day_courses:
                schedule_text.append(f"\n📅 {day_name}:")
                for course in day_courses:
                    schedule_text.append(f"  📚 {course['course_name']}")
                    schedule_text.append(f"     👨‍🏫 {course['teacher']}")
                    schedule_text.append(f"     📍 {course['location']}")
                    schedule_text.append(f"     ⏰ {course['time']}")

        return '\n'.join(schedule_text), course_list

    def _course_key(self, course):
        """生成课程唯一标识：星期+时间段+节次+课程名"""
        return f"{course['day']}|{course['time']}"

    def _course_detail_key(self, course):
        """生成课程完整信息key用于判断是否修改"""
        return f"{self._course_key(course)}|{course['course_name']}|{course['teacher']}|{course['location']}"

    def diff_schedules(self, old_courses, new_courses):
        """对比新旧课表，返回变化部分：新增、删除、修改的课程"""
        # 构建字典方便查找
        old_dict = {self._course_key(c): c for c in old_courses}
        new_dict = {self._course_key(c): c for c in new_courses}
        
        added = []    # 新增课程
        removed = []  # 删除课程
        modified = [] # 修改的课程

        # 检查新增和修改
        for key, new_course in new_dict.items():
            if key not in old_dict:
                added.append(new_course)
            else:
                old_course = old_dict[key]
                if self._course_detail_key(new_course) != self._course_detail_key(old_course):
                    modified.append((old_course, new_course))
        
        # 检查删除
        for key, old_course in old_dict.items():
            if key not in new_dict:
                removed.append(old_course)
        
        return added, removed, modified

    def format_diff_message(self, added, removed, modified):
        """格式化差异消息"""
        diff_parts = []
        
        if added:
            diff_parts.append("➕ 【新增课程】")
            for c in added:
                diff_parts.append(f"  📅 {c['day']} {c['period']}")
                diff_parts.append(f"     📚 {c['course_name']}")
                diff_parts.append(f"     👨‍🏫 {c['teacher']}")
                diff_parts.append(f"     📍 {c['location']}")
                diff_parts.append(f"     ⏰ {c['time']}")
                diff_parts.append("")

        if removed:
            diff_parts.append("➖ 【取消课程】")
            for c in removed:
                diff_parts.append(f"  📅 {c['day']} {c['period']}")
                diff_parts.append(f"     📚 {c['course_name']}")
                diff_parts.append(f"     👨‍🏫 {c['teacher']}")
                diff_parts.append(f"     📍 {c['location']}")
                diff_parts.append(f"     ⏰ {c['time']}")
                diff_parts.append("")

        if modified:
            diff_parts.append("✏️ 【调整课程】")
            for old_c, new_c in modified:
                diff_parts.append(f"  📅 {new_c['day']} {new_c['period']}")
                diff_parts.append(f"     ⏰ {new_c['time']}")
                # 显示具体哪些字段变了
                if old_c['course_name'] != new_c['course_name']:
                    diff_parts.append(f"     📚 课程：{old_c['course_name']} → {new_c['course_name']}")
                else:
                    diff_parts.append(f"     📚 课程：{new_c['course_name']}")
                if old_c['teacher'] != new_c['teacher']:
                    diff_parts.append(f"     👨‍🏫 教师：{old_c['teacher']} → {new_c['teacher']}")
                else:
                    diff_parts.append(f"     👨‍🏫 教师：{new_c['teacher']}")
                if old_c['location'] != new_c['location']:
                    diff_parts.append(f"     📍 教室：{old_c['location']} → {new_c['location']}")
                else:
                    diff_parts.append(f"     📍 教室：{new_c['location']}")
                diff_parts.append("")

        return '\n'.join(diff_parts) if diff_parts else "（无具体变化详情）"

    def load_last_courses(self):
        """加载上次保存的课程列表"""
        try:
            if os.path.exists(LAST_SCHEDULE_FILE):
                with open(LAST_SCHEDULE_FILE, 'r', encoding='utf-8') as f:
                    last_data = json.load(f)
                _, last_courses = self.parse_schedule(last_data)
                return last_courses
        except Exception as e:
            logger.warning(f"加载上次课程列表失败: {str(e)}")
        return None

    def _parse_course_dto(self, dto, day_name, period_name):
        """解析单个课程DTO"""
        course = {
            'day': day_name,
            'period': period_name,
            'course_name': '',
            'teacher': '',
            'location': '',
            'time': '',
            'class_name': dto.get('LessonObjName', '')
        }

        for item in dto.get('Content', []):
            key = item.get('Key', '')
            name = item.get('Name', '')
            if key == 'Lesson':
                course['course_name'] = name
            elif key == 'Teacher':
                course['teacher'] = name
            elif key == 'Room':
                course['location'] = name
            elif key == 'Time':
                course['time'] = name

        return course

    def calculate_hash(self, data):
        """计算课表数据的哈希值用于对比"""
        # 只提取关键课程信息进行哈希，避免参数变动导致误报
        _, course_list = self.parse_schedule(data)
        # 提取课程核心字段排序后计算哈希
        core_data = []
        for course in course_list:
            core_data.append({
                'course_name': course['course_name'],
                'teacher': course['teacher'],
                'location': course['location'],
                'time': course['time']
            })
        core_str = json.dumps(core_data, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(core_str.encode('utf-8')).hexdigest(), core_str

    def send_dingtalk_notification(self, message):
        """发送钉钉机器人消息"""
        webhook_url = DINGTALK_WEBHOOK

        # 如果配置了加签密钥，计算签名
        if DINGTALK_SECRET:
            import time
            import hmac
            import hashlib
            timestamp = str(round(time.time() * 1000))
            secret_enc = DINGTALK_SECRET.encode('utf-8')
            string_to_sign = f'{timestamp}\n{DINGTALK_SECRET}'
            string_to_sign_enc = string_to_sign.encode('utf-8')
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            webhook_url = f'{DINGTALK_WEBHOOK}&timestamp={timestamp}&sign={sign}'

        try:
            headers = {'Content-Type': 'application/json'}

            # 解析@的手机号
            at_mobiles = [m.strip() for m in DINGTALK_AT_MOBILES.split(',') if m.strip()] if DINGTALK_AT_MOBILES else []

            data = {
                "msgtype": "text",
                "text": {
                    "content": message
                },
                "at": {
                    "atMobiles": at_mobiles,
                    "isAtAll": DINGTALK_AT_ALL
                }
            }

            response = requests.post(
                webhook_url,
                headers=headers,
                data=json.dumps(data),
                timeout=10
            )
            result = response.json()

            if result.get('errcode') == 0:
                logger.info("钉钉消息发送成功")
                return True
            else:
                logger.error(f"钉钉消息发送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"发送钉钉消息异常: {str(e)}")
            return False

    def send_first_run_success(self, schedule_text):
        """首次运行成功，发送启动通知"""
        notify_msg = f"""✅【课表监控启动成功】
⏰ 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
👤 账号: {USERNAME[:3]}***

📋 已获取当前课表作为监控基线:
{schedule_text}

🔔 后续课表如有变化将立即通知您。"""
        self.send_dingtalk_notification(notify_msg)

    def send_error_notification(self, error_msg, error_traceback=""):
        """发送故障报警通知，包含错误日志"""
        # 获取最近的日志
        recent_logs = memory_log_handler.get_logs()[-2000:]  # 限制长度避免钉钉消息过长
        
        notify_msg = f"""❌【课表监控运行故障】
⏰ 故障时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🚨 错误信息:
{error_msg}

📝 最近运行日志:
```
{recent_logs}
```"""
        if error_traceback:
            notify_msg += f"""
🔍 错误堆栈:
```
{error_traceback[-1500:]}
```"""
        
        # 尝试发送，即使失败也不抛出异常
        try:
            self.send_dingtalk_notification(notify_msg)
        except:
            pass

    def load_last_state(self):
        """加载上次保存的课表状态"""
        try:
            if os.path.exists(LAST_HASH_FILE):
                with open(LAST_HASH_FILE, 'r', encoding='utf-8') as f:
                    last_hash = f.read().strip()
                if last_hash:
                    return last_hash
        except Exception as e:
            logger.warning(f"加载上次状态失败: {str(e)}")
        return None

    def save_current_state(self, current_hash, schedule_data):
        """保存当前课表状态"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(os.path.abspath(LAST_HASH_FILE)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.abspath(LAST_SCHEDULE_FILE)), exist_ok=True)

            with open(LAST_HASH_FILE, 'w', encoding='utf-8') as f:
                f.write(current_hash)

            with open(LAST_SCHEDULE_FILE, 'w', encoding='utf-8') as f:
                json.dump(schedule_data, f, ensure_ascii=False, indent=2)

            logger.info("当前课表状态已保存")
        except Exception as e:
            logger.error(f"保存状态失败: {str(e)}")

    def run_once(self):
        """执行一次监控检查
        返回: (success, changed)
            - success: 本次检查是否成功执行
            - changed: 课表是否有变化（用于外部决定是否需要git提交等）
        """
        logger.info("=" * 50)
        logger.info(f"开始执行课表监控 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # 1. 登录
        if not self.login():
            return False, False

        # 2. 获取课表
        schedule_data = self.get_schedule()
        if not schedule_data:
            return False, False

        # 3. 计算当前哈希
        current_hash, core_str = self.calculate_hash(schedule_data)
        logger.info(f"当前课表哈希: {current_hash}")

        # 4. 加载上次状态
        last_hash = self.load_last_state()

        # 5. 对比是否有变化
        if last_hash is None:
            # 首次运行，保存状态并发送启动成功通知
            logger.info("首次运行，已初始化课表基线")
            schedule_text, _ = self.parse_schedule(schedule_data)
            logger.info(f"当前课表:\n{schedule_text}")
            self.save_current_state(current_hash, schedule_data)
            
            # 发送首次运行成功通知
            logger.info("发送首次运行成功通知")
            self.send_first_run_success(schedule_text)
            
            return True, True  # 首次也算"有变化"（新基线），方便外部持久化

        if current_hash == last_hash:
            logger.info("课表无变化，不发送消息")
            return True, False

        # 6. 课表有变化，发送通知
        logger.info("检测到课表变化！")
        schedule_text, course_list = self.parse_schedule(schedule_data)
        last_courses = self.load_last_courses()
        
        # 对比差异
        if last_courses:
            added, removed, modified = self.diff_schedules(last_courses, course_list)
            diff_text = self.format_diff_message(added, removed, modified)
            change_count = len(added) + len(removed) + len(modified)
            logger.info(f"共发现 {change_count} 处变化: 新增{len(added)}门, 删除{len(removed)}门, 调整{len(modified)}门")
            logger.info(f"变化详情:\n{diff_text}")
        else:
            diff_text = "（无法加载上次课表基线，无法对比详细变化）"
            change_count = 0

        # 构造通知消息 - 仅发送变化部分
        notify_msg = f"""🔔【教务系统课表变更提醒】
⏰ 检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔄 共 {change_count} 处变化

📋 变化详情:
{diff_text}
⚠️ 请及时查看确认课程安排调整!"""

        # 发送钉钉通知
        self.send_dingtalk_notification(notify_msg)

        # 保存新状态
        self.save_current_state(current_hash, schedule_data)

        return True, True


def main():
    """主函数"""
    print("=" * 50)
    print("教务系统课表监控程序")
    print("=" * 50)
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 清空日志缓存
    memory_log_handler.clear()

    # 支持命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == '--test':
            # 测试模式：立即发送测试消息
            if not DINGTALK_WEBHOOK:
                print("❌ 请先配置环境变量 DINGTALK_WEBHOOK")
                sys.exit(1)
            monitor = ScheduleMonitor()
            success = monitor.send_dingtalk_notification(
                "🔔【测试消息】教务系统课表监控程序已部署成功！\n\n这是一条测试消息，确认钉钉机器人配置正确。"
            )
            sys.exit(0 if success else 1)

        if sys.argv[1] == '--env':
            # 显示当前环境变量配置（隐去敏感信息）
            print("当前环境变量配置：")
            print(f"  STU_USERNAME:   {USERNAME[:3] + '***' if USERNAME else '(未设置)'}")
            print(f"  STU_PASSWORD:   {'***' if PASSWORD else '(未设置)'}")
            print(f"  STU_BASE_URL:   {BASE_URL}")
            print(f"  DINGTALK_WEBHOOK: {'已设置' if DINGTALK_WEBHOOK else '(未设置)'}")
            print(f"  DINGTALK_SECRET: {'已设置' if DINGTALK_SECRET else '(未设置)'}")
            print(f"  LAST_HASH_FILE: {LAST_HASH_FILE}")
            print(f"  LAST_SCHEDULE_FILE: {LAST_SCHEDULE_FILE}")
            print(f"  LOG_LEVEL:      {LOG_LEVEL}")
            sys.exit(0)

        if sys.argv[1] == '--now':
            # 立即执行一次（忽略历史基线，强制触发一次完整检查）
            if not check_required_env():
                # 发送环境变量缺失错误通知
                try:
                    monitor = ScheduleMonitor()
                    monitor.send_error_notification("环境变量配置缺失，请检查必填参数是否正确设置")
                except:
                    pass
                sys.exit(1)
            try:
                # 清空历史基线
                if os.path.exists(LAST_HASH_FILE):
                    os.remove(LAST_HASH_FILE)
                if os.path.exists(LAST_SCHEDULE_FILE):
                    os.remove(LAST_SCHEDULE_FILE)
                monitor = ScheduleMonitor()
                success, changed = monitor.run_once()
                print(f"\n执行结果: {'成功' if success else '失败'} | 课表变化: {'是' if changed else '否'}")
                # 输出标记供外部脚本判断
                print(f"::set-output name=success::{str(success).lower()}")
                print(f"::set-output name=changed::{str(changed).lower()}")
                
                if not success:
                    monitor.send_error_notification("课表检查执行失败，请查看日志详情")
                    
                sys.exit(0 if success else 1)
            except Exception as e:
                error_msg = f"程序运行发生异常: {str(e)}"
                tb = traceback.format_exc()
                logger.error(error_msg)
                logger.error(tb)
                try:
                    monitor = ScheduleMonitor()
                    monitor.send_error_notification(error_msg, tb)
                except:
                    pass
                sys.exit(1)

    # 正常执行一次检查（默认模式，供 cron-job 调用）
    if not check_required_env():
        # 发送环境变量缺失错误通知
        try:
            monitor = ScheduleMonitor()
            monitor.send_error_notification("环境变量配置缺失，请检查必填参数是否正确设置")
        except:
            pass
        sys.exit(1)

    try:
        monitor = ScheduleMonitor()
        success, changed = monitor.run_once()

        print(f"\n执行结果: {'成功' if success else '失败'} | 课表变化: {'是' if changed else '否'}")
        # 输出 GitHub Actions 可用的 output
        print(f"::set-output name=success::{str(success).lower()}")
        print(f"::set-output name=changed::{str(changed).lower()}")

        if not success:
            monitor.send_error_notification("课表检查执行失败：登录或获取课表异常，请检查网络或账号密码")
            
        sys.exit(0 if success else 1)
        
    except Exception as e:
        error_msg = f"程序运行发生未捕获异常: {str(e)}"
        tb = traceback.format_exc()
        logger.error(error_msg)
        logger.error(tb)
        # 发送故障通知
        try:
            monitor = ScheduleMonitor()
            monitor.send_error_notification(error_msg, tb)
        except:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
