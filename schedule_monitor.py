#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
教务系统课表监控程序
功能：登录教务系统，监控课表变化，通过钉钉机器人发送提醒
可手动运行，也可配合外部 cron-job 每隔10分钟自动运行

所有配置均通过环境变量传入，详见 README.md
"""

import requests
import base64
import urllib.parse
import json
import hashlib
import os
import sys
import logging
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
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
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
            self.session.get(f'{BASE_URL}/Login', timeout=10)

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
                timeout=10
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

    def get_schedule(self):
        """获取当前课表数据"""
        try:
            schedule_data = {'XQJC': ''}
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
                timeout=10
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
            # 首次运行，只保存状态不发消息
            logger.info("首次运行，已初始化课表基线")
            schedule_text, _ = self.parse_schedule(schedule_data)
            logger.info(f"当前课表:\n{schedule_text}")
            self.save_current_state(current_hash, schedule_data)
            return True, True  # 首次也算"有变化"（新基线），方便外部持久化

        if current_hash == last_hash:
            logger.info("课表无变化，不发送消息")
            return True, False

        # 6. 课表有变化，发送通知
        logger.info("检测到课表变化！")
        schedule_text, course_list = self.parse_schedule(schedule_data)

        # 构造通知消息
        notify_msg = f"""🔔【教务系统课表变更提醒】
⏰ 检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

📋 最新课表信息:
{schedule_text}

⚠️ 请及时查看确认课程安排是否已调整!"""

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
                sys.exit(1)
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
            sys.exit(0 if success else 1)

    # 正常执行一次检查（默认模式，供 cron-job 调用）
    if not check_required_env():
        sys.exit(1)

    monitor = ScheduleMonitor()
    success, changed = monitor.run_once()

    print(f"\n执行结果: {'成功' if success else '失败'} | 课表变化: {'是' if changed else '否'}")
    # 输出 GitHub Actions 可用的 output
    print(f"::set-output name=success::{str(success).lower()}")
    print(f"::set-output name=changed::{str(changed).lower()}")

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
