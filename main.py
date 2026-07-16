"""
AstrBot 智能提醒插件 (v2.5.0)
支持：
1. 日常提醒 - 引用消息/自然语言设置提醒，支持人名模糊识别(@别名→QQ号)
2. 游戏组局召集 - "海不海"@分组群友，"康康、阿朱海不海"额外@指定人
3. 游戏上号提醒 - 引用时间消息设置提前5分钟提醒，可指定@目标人
4. 生日祝贺 - 生日当天指定时间自动祝贺

每个功能可独立开关：enable_reminder / enable_gaming / enable_birthday
时间处理：显式使用 timezone 配置，不依赖系统时区（解决 UTC 容器问题）
"""

import asyncio
import json
import re
import time
import uuid
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools

WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

# [at:xxx] 标签正则，用于将文本中的 @ 标签转为 At 组件
AT_TAG_PATTERN = re.compile(r"\[at:(\d+|all)\]")


@register("smart_reminder", "user", "智能提醒插件 - 支持提醒、游戏组局召集、生日祝贺", "2.5.0")
class SmartReminderPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # ========== 功能开关（三分类）==========
        self.enable_reminder: bool = config.get("enable_reminder", True)
        self.enable_gaming: bool = config.get("enable_gaming", True)
        self.enable_birthday: bool = config.get("enable_birthday", True)

        # ========== 全局配置 ==========
        self.timezone_str = config.get("timezone", "Asia/Shanghai")
        self.tz = zoneinfo.ZoneInfo(self.timezone_str)
        self.enable_llm_tool = config.get("enable_llm_tool", True)
        self.inject_time_ctx = config.get("inject_time_context", True)

        # ========== 安全类型转换（WebUI 可能传入字符串）==========
        self.poll_interval = int(config.get("poll_interval", 30))
        self.reminder_advance = int(config.get("reminder_advance_minutes", 5))

        # ========== 联系人称呼别名 ==========
        self.contact_aliases: list = config.get("contact_aliases", [])
        # 构建 别名→QQ号 快速查找表
        self._alias_to_qq: dict = {}
        for entry in self.contact_aliases:
            qq = entry.get("qq", "")
            aliases = entry.get("aliases", [])
            for alias in aliases:
                self._alias_to_qq[alias] = qq

        # ========== 游戏分组配置 ==========
        # template_list 在配置中是 list 格式，内部转为 dict 以便按名称查找
        gaming_groups_raw: list = config.get("gaming_groups", [])
        self.gaming_groups: dict = {}
        for entry in gaming_groups_raw:
            gname = entry.get("group_name", "")
            if gname:
                self.gaming_groups[gname] = {
                    "qq_list": entry.get("qq_list", []),
                    "aliases": entry.get("aliases", [])
                }

        # ========== 生日祝贺配置 ==========
        self.birthday_list: list = config.get("birthday_list", [])
        self.birthday_message: str = config.get(
            "birthday_message",
            "🎂 今天是 [at:{qq}] 的生日！祝你生日快乐，新的一年冒险等阶大涨！"
        )
        # 解析发送时间（HH:MM），兼容旧版 birthday_send_hour
        send_time_str = str(config.get("birthday_send_time", "")).strip()
        if not send_time_str:
            # 旧版兼容：birthday_send_hour (int) → "HH:00"
            old_hour = int(config.get("birthday_send_hour", 0))
            send_time_str = f"{old_hour:02d}:00"
        try:
            parts = send_time_str.split(":")
            self.birthday_send_hour = int(parts[0])
            self.birthday_send_minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            self.birthday_send_hour = 0
            self.birthday_send_minute = 0

        # 数据持久化
        self.data_dir = StarTools.get_data_dir()
        self.reminders_file = self.data_dir / "reminders.json"
        self.birthday_sent_file = self.data_dir / "birthday_sent.json"
        self.reminders: dict = self._load_json(self.reminders_file)
        self.birthday_sent: dict = self._load_json(self.birthday_sent_file)

        # 启动后台轮询
        self._poll_task = asyncio.create_task(self._polling_loop())
        enabled = []
        if self.enable_reminder:
            enabled.append("日常提醒")
        if self.enable_gaming:
            enabled.append("游戏提醒")
        if self.enable_birthday:
            enabled.append("生日祝福")
        logger.info(
            "[SmartReminder] 插件已加载 v2.5.0，时区=%s，当前时间=%s，轮询间隔 %d 秒，已启用: %s，分组: %s，别名: %d 人",
            self.timezone_str, self._now().strftime("%Y-%m-%d %H:%M:%S"),
            self.poll_interval, "、".join(enabled) if enabled else "无",
            list(self.gaming_groups.keys()) if self.enable_gaming else "（未启用）",
            len(self.contact_aliases) if self.enable_reminder else 0
        )
        if self.enable_birthday:
            logger.info(
                "[SmartReminder] 生日祝贺: 发送时间=%02d:%02d, 名单=%d人",
                self.birthday_send_hour, self.birthday_send_minute, len(self.birthday_list)
            )

    async def terminate(self):
        """插件卸载时取消后台任务"""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("[SmartReminder] 插件已卸载")

    # ============================================================
    # 时间工具 - 使用配置的时区
    # ============================================================

    def _now(self) -> datetime:
        """获取当前时间（使用配置的时区，不依赖系统时区）"""
        return datetime.now(self.tz)

    # ============================================================
    # 数据管理
    # ============================================================

    def _load_json(self, filepath: Path) -> dict:
        """从 JSON 文件加载数据"""
        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error("[SmartReminder] 加载 %s 失败: %s", filepath, e)
        return {}

    async def _save_json(self, filepath: Path, data: dict):
        """异步保存数据到 JSON 文件"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("[SmartReminder] 保存 %s 失败: %s", filepath, e)

    async def _save_reminders(self):
        await self._save_json(self.reminders_file, self.reminders)

    async def _save_birthday_sent(self):
        await self._save_json(self.birthday_sent_file, self.birthday_sent)

    # ============================================================
    # LLM 请求钩子 - 注入时间上下文、引用消息、游戏分组信息
    # ============================================================

    @filter.on_llm_request()
    async def inject_time_context(self, event: AstrMessageEvent, req):
        """在 LLM 请求中注入上下文信息"""
        if not self.inject_time_ctx:
            return

        now = self._now()
        weekday_cn = WEEKDAY_CN[now.weekday()]

        # 基础时间上下文
        time_info = (
            f"\n[SmartReminder 当前时间上下文]\n"
            f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} 星期{weekday_cn}\n"
        )

        # 引用消息
        quoted_text = ""
        for comp in event.get_messages():
            if isinstance(comp, Reply) and comp.message_str:
                quoted_text = comp.message_str

        if quoted_text:
            time_info += (
                f'用户引用了以下消息: "{quoted_text}"\n'
            )

        # 提醒相关说明（仅在开启时注入）
        if self.enable_reminder:
            time_info += (
                f"set_reminder 工具: trigger_type 为 'absolute' 时使用 YYYY-MM-DD HH:MM，"
                f"为 'relative' 时填写整数分钟数。\n"
                f"重要规则: 当用户要求设置提醒时，"
                f"你必须在同一轮回复中同时用你的人格风格生成一句简短的确认文案（如「好的，收到委托！」）"
                f"并调用工具，不要分两轮回复。\n"
            )

        # 联系人别名对照表（日常提醒或游戏提醒开启时都注入）
        should_inject_aliases = (self.enable_reminder or self.enable_gaming) and self.contact_aliases
        if should_inject_aliases:
            alias_lines = []
            for entry in self.contact_aliases:
                qq = entry.get("qq", "")
                aliases = entry.get("aliases", [])
                alias_lines.append(
                    f"- 称呼「{', '.join(aliases)}」 → QQ号 {qq}，@标签 [at:{qq}]"
                )
            alias_usage_parts = []
            if self.enable_reminder:
                alias_usage_parts.append(
                    "set_reminder 的 target_qq 参数（提醒某人时填写其QQ号）"
                )
            if self.enable_gaming:
                alias_usage_parts.append(
                    "call_gamers 的 extra_names 参数（额外@某人时填写其称呼，逗号分隔）"
                )
                alias_usage_parts.append(
                    "set_gaming_reminder 的 target_qq 参数（提醒某人上号时填写其QQ号）"
                )
            time_info += (
                f"\n[联系人称呼别名]\n"
                f"当用户提到某人的称呼（如「康康」「阿朱」「啊哈」「小A」）时，请从以下对照表中查找对应的QQ号，\n"
                f"用途: {'; '.join(alias_usage_parts)}。\n"
                + "\n".join(alias_lines) + "\n"
                f"如果用户提到的称呼不在对照表中，对应参数不要填写（留空或不传）。\n"
            )

        # 游戏分组信息（仅在开启时注入）
        if self.enable_gaming and self.gaming_groups:
            group_desc_lines = []
            for name, grp in self.gaming_groups.items():
                qq_list = grp.get("qq_list", [])
                aliases = grp.get("aliases", [])
                qq_at_tags = " ".join(f"[at:{qq}]" for qq in qq_list)
                group_desc_lines.append(
                    f"- 分组「{name}」: QQ号 {', '.join(qq_list)}, "
                    f"别名/关键词: {', '.join(aliases)}, "
                    f"@标签: {qq_at_tags}"
                )
            time_info += (
                f"\n[游戏组局召集]\n"
                f"当前配置的游戏分组:\n"
                + "\n".join(group_desc_lines) + "\n"
                f"当用户只说「海不海」「瓦不瓦」时 → 只 @ 该分组的人，"
                f"调用 call_gamers(group_name=\"海\")，不填 extra_names。\n"
                f"当用户说「康康、阿朱海不海」时 → @ 分组的人 + 额外 @ 康康和阿朱，"
                f"从别名对照表查到QQ号后，"
                f"调用 call_gamers(group_name=\"海\", extra_names=\"康康,阿朱\")。\n"
                f"工具会返回需要 @ 的人的 [at:xxx] 标签，你在回复中保留这些标签，"
                f"并用你的人格风格生成召集文案。\n"
                f"当用户引用含时间的消息并要求「提醒大家上号」时，"
                f"请从引用消息中解析出时间，减去 {self.reminder_advance} 分钟，"
                f"调用 set_gaming_reminder 工具设置提醒。"
                f"同时，请用你的人格风格生成一段提醒文案作为 reminder_message 参数，"
                f"这段文案会在提醒触发时发送到群里。\n"
                f"如果用户提到特定的人（如「提醒康康上号」），请从别名对照表查找QQ号，"
                f"填入 set_gaming_reminder 的 target_qq 参数，提醒触发时会 @ 该人。\n"
            )

        if req.system_prompt:
            req.system_prompt += time_info
        else:
            req.system_prompt = time_info

    # ============================================================
    # LLM 工具 - 普通提醒
    # ============================================================

    @filter.llm_tool(name="set_reminder")
    async def set_reminder(
        self,
        event: AstrMessageEvent,
        reminder_content: str,
        trigger_type: str,
        trigger_value: str,
        target_qq: str = "",
    ):
        """设置提醒。支持相对时间和绝对时间两种方式。可以指定提醒的目标人物。

        你必须在同一轮回复中同时用你的人格风格生成一句简短的确认文案
        （如「好的，收到委托！到时间会通知你的~」）并调用此工具。
        私聊中你的确认文案就是最终回复；群聊中工具会自行发送一条简短的确认。
        只有当工具返回错误信息时，才需要告知用户。

        当用户说「提醒我30分钟后抢票」时，使用 relative 类型，trigger_value 填写分钟数。
        当用户引用消息说「提前2小时提醒」时，从引用消息中解析出绝对时间，减去提前量，
        使用 absolute 类型，trigger_value 填写 YYYY-MM-DD HH:MM 格式的日期时间。
        当用户说「明天下午3点提醒我开会」时，使用 absolute 类型。
        当用户说「提醒啊哈上线」时，从别名对照表中查找「啊哈」对应的QQ号，填入 target_qq。
        提醒触发时会自动 @ target_qq 对应的人。

        Args:
            reminder_content(string): 提醒的内容，例如"抢票""新街口见面""上线"
            trigger_type(string): 提醒触发方式。"relative"表示从现在起的分钟数，"absolute"表示具体日期时间
            trigger_value(string): 提醒触发时间值。"relative"时填整数分钟数如"30"，"absolute"时填日期时间如"2025-07-19 12:00"
            target_qq(string): 提醒的目标人物QQ号。从别名对照表中查找称呼对应的QQ号填入。如果没有指定目标人物，留空或不传。
        """
        if not self.enable_reminder:
            return "日常提醒功能未启用，无法设置提醒。"
        now = self._now()

        if trigger_type == "relative":
            minutes = self._parse_minutes(trigger_value)
            if minutes is None:
                return "无法解析分钟数，请使用纯数字，如 '30'。"
            trigger_dt = now + timedelta(minutes=minutes)

        elif trigger_type == "absolute":
            trigger_dt = self._parse_absolute_time(trigger_value, now)
            if trigger_dt is None:
                return (
                    "无法解析日期时间。请使用 YYYY-MM-DD HH:MM 格式，"
                    f"当前时间为 {now.strftime('%Y-%m-%d %H:%M')}。"
                )
        else:
            return "trigger_type 必须为 'relative' 或 'absolute'。"

        if trigger_dt <= now:
            return f"提醒时间已过，无法设置。请设置未来的时间。"

        reminder_id = uuid.uuid4().hex[:8]
        umo = event.unified_msg_origin
        is_group = event.get_group_id() != ""
        group_id = event.get_group_id()

        self.reminders[reminder_id] = {
            "id": reminder_id,
            "content": reminder_content,
            "trigger_timestamp": trigger_dt.timestamp(),
            "trigger_time_str": trigger_dt.strftime("%Y-%m-%d %H:%M"),
            "target_umo": umo,
            "is_group": is_group,
            "group_id": group_id,
            "created_by_id": event.get_sender_id(),
            "created_by_name": event.get_sender_name() or event.get_sender_id(),
            "created_at": now.timestamp(),
            "created_at_str": now.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
            "type": "normal",
            "target_qq": target_qq,
        }
        await self._save_reminders()

        logger.info(
            "[SmartReminder] 提醒已创建: id=%s, content=%s, trigger=%s, umo=%s",
            reminder_id, reminder_content, trigger_dt.strftime("%Y-%m-%d %H:%M"), umo
        )

        # 私聊中 LLM 会自己生成确认回复（如"好的，收到委托！"），不重复发送
        # 群聊中 LLM 通常只生成工具调用不生成文字，需要工具自己发确认
        if is_group:
            time_str = trigger_dt.strftime("%Y-%m-%d %H:%M")
            confirm_msg = f"⏰ 收到委托！到 {time_str} 会通知你的~"
            await self.context.send_message(umo, MessageChain().message(confirm_msg))
        return None

    # ============================================================
    # LLM 工具 - 游戏组局召集
    # ============================================================

    @filter.llm_tool(name="call_gamers")
    async def call_gamers(
        self,
        event: AstrMessageEvent,
        group_name: str,
        extra_names: str = "",
    ):
        """召集游戏分组里的群友，可同时额外@指定的人。

        当用户只说「海不海」时，只 @ 该分组的人（不填 extra_names）。
        当用户说「康康、阿朱海不海」时，除了 @ 分组的人，还要 @ 康康和阿朱。
        请从别名对照表中查找「康康」「阿朱」对应的QQ号，填入 extra_names（逗号分隔）。

        工具返回需要 @ 的人的标签，你必须在回复中保留这些 [at:xxx] 标签，
        并用你的人格风格生成召集文案（如"旅行者们，今晚的海克斯大乱斗委托已开放"）。

        Args:
            group_name(string): 游戏分组名称，如"海""瓦"。必须与配置中的分组名称匹配。
            extra_names(string): 额外需要@的人的称呼，逗号分隔，如"康康,阿朱"。请从别名对照表中查找称呼对应的QQ号填入。如果用户没有额外指定人，留空或不传。
        """
        if not self.enable_gaming:
            return "游戏提醒功能未启用，无法召集游戏分组。"
        group_config = self.gaming_groups.get(group_name)

        # 如果精确匹配失败，尝试别名匹配
        if not group_config:
            for name, grp in self.gaming_groups.items():
                aliases = grp.get("aliases", [])
                if group_name in aliases or any(a in group_name for a in aliases):
                    group_config = grp
                    group_name = name
                    break

        if not group_config:
            available = ", ".join(self.gaming_groups.keys())
            return f"未找到分组「{group_name}」。当前可用分组: {available}"

        # 收集分组内的 QQ 号
        group_qq_list = group_config.get("qq_list", [])
        all_qq = set(group_qq_list)

        # 解析额外人名（从别名对照表查找QQ号）
        unresolved = []
        if extra_names:
            for name in extra_names.split(","):
                name = name.strip()
                if not name:
                    continue
                qq = self._alias_to_qq.get(name)
                if qq:
                    all_qq.add(qq)
                else:
                    unresolved.append(name)

        if not all_qq:
            return f"分组「{group_name}」中没有成员，且额外人名未找到对应QQ号。"

        # 返回 @ 标签，LLM 在回复中保留这些标签
        at_tags = " ".join(f"[at:{qq}]" for qq in all_qq)

        # 如果有未解析的人名，提示 LLM
        if unresolved:
            at_tags += f"（注意：以下称呼未在别名表中找到QQ号，无法@：{', '.join(unresolved)}）"

        return at_tags

    # ============================================================
    # LLM 工具 - 游戏上号提醒
    # ============================================================

    @filter.llm_tool(name="set_gaming_reminder")
    async def set_gaming_reminder(
        self,
        event: AstrMessageEvent,
        trigger_time: str,
        reminder_message: str,
        target_qq: str = "",
    ):
        """设置游戏上号提醒。从引用消息中解析出的时间减去提前量后设置提醒。

        你必须在同一轮回复中同时用你的人格风格生成一句简短的确认文案并调用此工具。
        私聊中你的确认文案就是最终回复；群聊中工具会自行发送一条简短的确认。
        你还需要用你的人格风格生成一段提醒文案作为 reminder_message 参数，
        这段文案会在提醒触发时发送到群里。
        如果用户提到了特定的人（如"提醒康康上号"），请从别名对照表中查找QQ号填入 target_qq，
        提醒触发时会 @ 该人。

        Args:
            trigger_time(string): 游戏开始时间，YYYY-MM-DD HH:MM 格式。工具会自动减去提前量。
            reminder_message(string): 提醒触发时发送的文案，用你的人格风格写，如"还有5分钟上号了，旅行者们准备好"
            target_qq(string): 提醒触发时需要@的目标人QQ号。从别名对照表中查找称呼对应的QQ号填入。如果没有指定目标人物，留空或不传，届时只发纯文本提醒。
        """
        if not self.enable_gaming:
            return "游戏提醒功能未启用，无法设置游戏提醒。"
        now = self._now()

        # 解析游戏开始时间
        start_dt = self._parse_absolute_time(trigger_time, now)
        if start_dt is None:
            return f"无法解析时间 {trigger_time}，请使用 YYYY-MM-DD HH:MM 格式。"

        # 提前 N 分钟
        trigger_dt = start_dt - timedelta(minutes=self.reminder_advance)
        if trigger_dt <= now:
            return f"提醒时间已过（需提前{self.reminder_advance}分钟），无法设置。"

        reminder_id = uuid.uuid4().hex[:8]
        umo = event.unified_msg_origin
        is_group = event.get_group_id() != ""
        group_id = event.get_group_id()

        self.reminders[reminder_id] = {
            "id": reminder_id,
            "content": reminder_message,
            "trigger_timestamp": trigger_dt.timestamp(),
            "trigger_time_str": trigger_dt.strftime("%Y-%m-%d %H:%M"),
            "target_umo": umo,
            "is_group": is_group,
            "group_id": group_id,
            "created_by_id": event.get_sender_id(),
            "created_by_name": event.get_sender_name() or event.get_sender_id(),
            "created_at": now.timestamp(),
            "created_at_str": now.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
            "type": "gaming",
            "target_qq": target_qq,
        }
        await self._save_reminders()

        logger.info(
            "[SmartReminder] 游戏提醒已创建: id=%s, start=%s, remind=%s, umo=%s",
            reminder_id, start_dt.strftime("%Y-%m-%d %H:%M"),
            trigger_dt.strftime("%Y-%m-%d %H:%M"), umo
        )

        # 私聊中 LLM 会自己生成确认回复，不重复发送
        # 群聊中 LLM 通常只生成工具调用不生成文字，需要工具自己发确认
        if is_group:
            confirm_msg = (
                f"🎮 收到委托！游戏 {start_dt.strftime('%H:%M')} 开始，"
                f"会提前 {self.reminder_advance} 分钟通知大家~"
            )
            await self.context.send_message(umo, MessageChain().message(confirm_msg))
        return None

    # ============================================================
    # 消息渲染拦截器 - [at:xxx] 标签转为真实 @ 组件
    # ============================================================

    @filter.on_decorating_result(priority=2)
    async def convert_at_tags(self, event: AstrMessageEvent):
        """将 LLM 输出中的 [at:xxx] 标签转换为 OneBot 的 At 消息组件"""
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否包含 at 标签
        has_tag = any(
            isinstance(comp, Plain) and "[at:" in comp.text
            for comp in result.chain
        )
        if not has_tag:
            return

        new_chain = []

        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                text = comp.text
                last_idx = 0

                for match in AT_TAG_PATTERN.finditer(text):
                    start, end = match.span()

                    # 标签之前的纯文本
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))

                    target_id = match.group(1)
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain(" "))  # @ 后加空格防粘连

                    last_idx = end

                # 剩余纯文本
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                new_chain.append(comp)

        # 注入零宽字符防止 @ 后文本粘连
        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                found_plain = False
                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = (
                            "\u200b" + new_chain[next_idx].text
                        )
                        found_plain = True
                        break
                if not found_plain:
                    new_chain.insert(idx + 1, Plain("\u200b"))
            idx += 1

        result.chain = new_chain

    # ============================================================
    # 指令 - 手动设置 / 查看 / 取消提醒
    # ============================================================

    @filter.command_group("remind")
    async def remind_group(self, event: AstrMessageEvent):
        """提醒指令组"""
        pass

    @remind_group.command("set")
    async def remind_set(
        self, event: AstrMessageEvent, minutes: int, content: str = "提醒"
    ):
        """设置相对时间提醒

        Args:
            minutes(number): 几分钟后提醒，例如 30
            content(string): 提醒内容，例如 抢票
        """
        if not self.enable_reminder:
            yield event.plain_result("日常提醒功能未启用。")
            return

        if minutes <= 0:
            yield event.plain_result("分钟数必须大于 0。")
            return

        now = self._now()
        trigger_dt = now + timedelta(minutes=minutes)

        reminder_id = uuid.uuid4().hex[:8]
        umo = event.unified_msg_origin

        self.reminders[reminder_id] = {
            "id": reminder_id,
            "content": content,
            "trigger_timestamp": trigger_dt.timestamp(),
            "trigger_time_str": trigger_dt.strftime("%Y-%m-%d %H:%M"),
            "target_umo": umo,
            "is_group": event.get_group_id() != "",
            "group_id": event.get_group_id(),
            "created_by_id": event.get_sender_id(),
            "created_by_name": event.get_sender_name() or event.get_sender_id(),
            "created_at": now.timestamp(),
            "created_at_str": now.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
            "type": "normal",
        }
        await self._save_reminders()

        time_str = trigger_dt.strftime("%Y-%m-%d %H:%M")
        yield event.plain_result(
            f"提醒已设置！\n"
            f"内容: {content}\n"
            f"提醒时间: {time_str}（{minutes}分钟后）\n"
            f"提醒ID: {reminder_id}"
        )

    @remind_group.command("list")
    async def remind_list(self, event: AstrMessageEvent):
        """查看当前会话的所有提醒"""
        if not self.enable_reminder:
            yield event.plain_result("日常提醒功能未启用。")
            return

        umo = event.unified_msg_origin
        now = self._now()

        my_reminders = [
            r for r in self.reminders.values()
            if r.get("target_umo") == umo and r.get("status") == "pending"
        ]

        if not my_reminders:
            yield event.plain_result("当前没有待触发的提醒。")
            return

        lines = ["📋 当前提醒列表:"]
        for r in sorted(my_reminders, key=lambda x: x["trigger_timestamp"]):
            remaining = r["trigger_timestamp"] - now.timestamp()
            remaining_str = self._format_duration(timedelta(seconds=max(0, remaining)))
            r_type = "游戏" if r.get("type") == "gaming" else "普通"
            lines.append(
                f"  [{r['id']}] ({r_type}) {r['content']}\n"
                f"    时间: {r['trigger_time_str']}（{remaining_str}后）\n"
                f"    创建者: {r['created_by_name']}"
            )

        yield event.plain_result("\n".join(lines))

    @remind_group.command("cancel")
    async def remind_cancel(self, event: AstrMessageEvent, reminder_id: str):
        """取消提醒

        Args:
            reminder_id(string): 要取消的提醒ID
        """
        if not self.enable_reminder:
            yield event.plain_result("日常提醒功能未启用。")
            return

        umo = event.unified_msg_origin

        if reminder_id not in self.reminders:
            yield event.plain_result(f"未找到提醒 {reminder_id}。")
            return

        r = self.reminders[reminder_id]
        if r.get("target_umo") != umo:
            yield event.plain_result("只能取消当前会话中的提醒。")
            return

        content = r["content"]
        self.reminders.pop(reminder_id, None)
        await self._save_reminders()

        yield event.plain_result(f"已取消提醒 [{reminder_id}]: {content}")

    # ============================================================
    # 后台轮询 - 检查提醒 + 生日祝贺
    # ============================================================

    async def _polling_loop(self):
        """后台轮询循环"""
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                try:
                    if self.enable_reminder:
                        await self._check_reminders()
                    if self.enable_birthday:
                        await self._check_birthdays()
                except Exception as e:
                    logger.error("[SmartReminder] 轮询检查异常: %s", e)
        except asyncio.CancelledError:
            pass

    async def _check_reminders(self):
        """检查并触发提醒"""
        now_ts = time.time()
        triggered_ids = []

        for rid, reminder in list(self.reminders.items()):
            if reminder.get("status") != "pending":
                continue
            if now_ts < reminder["trigger_timestamp"]:
                continue

            # 标记为触发中（防重复）
            reminder["status"] = "triggering"
            await self._save_reminders()

            try:
                await self._send_reminder(reminder)
                triggered_ids.append(rid)
            except Exception as e:
                logger.error("[SmartReminder] 发送提醒 %s 失败: %s", rid, e)
                reminder["status"] = "pending"
                await self._save_reminders()

        # 清理已成功触发的提醒
        for rid in triggered_ids:
            self.reminders.pop(rid, None)
        if triggered_ids:
            await self._save_reminders()

    async def _send_reminder(self, reminder: dict):
        """发送提醒消息"""
        umo = reminder["target_umo"]
        content = reminder["content"]
        is_group = reminder.get("is_group", False)
        r_type = reminder.get("type", "normal")
        created_by_name = reminder.get("created_by_name", "")
        created_by_id = reminder.get("created_by_id", "")
        target_qq = reminder.get("target_qq", "")

        if r_type == "gaming":
            # 游戏上号提醒
            msg = MessageChain()
            if is_group and target_qq:
                # 指定了目标人 → @ 他
                msg = msg.at(target_qq, target_qq)
                msg = msg.message(f" {content}")
            else:
                # 纯文本，不 @ 人
                msg = msg.message(content)
            await self.context.send_message(umo, msg)
            logger.info("[SmartReminder] 游戏提醒 %s 已发送至 %s", reminder["id"], umo)
        else:
            # 普通提醒
            msg = MessageChain()

            # 如果指定了 target_qq，优先 @ 目标人
            if is_group and target_qq:
                msg = msg.at(target_qq, target_qq)
                msg = msg.message(
                    f" ⏰ 提醒！\n"
                    f"内容: {content}\n"
                    f"提醒时间: {reminder.get('trigger_time_str', '')}"
                )
            elif is_group and created_by_id:
                # 没有 target_qq 时，群聊中 @ 设置者
                msg = msg.at(created_by_name, created_by_id)
                msg = msg.message(
                    f" ⏰ 提醒！\n"
                    f"内容: {content}\n"
                    f"提醒时间: {reminder.get('trigger_time_str', '')}\n"
                    f"（由 {created_by_name} 设置的提醒）"
                )
            else:
                msg = msg.message(
                    f"⏰ 提醒！\n"
                    f"内容: {content}\n"
                    f"提醒时间: {reminder.get('trigger_time_str', '')}"
                )

            await self.context.send_message(umo, msg)
            logger.info("[SmartReminder] 提醒 %s 已发送至 %s", reminder["id"], umo)

    async def _check_birthdays(self):
        """检查是否有今天需要发送生日祝贺的人。

        触发条件：当前时间在设定时间的 2 分钟窗口内（防止 30 秒轮询跳过整点）。
        防重复：通过 birthday_sent.json 记录，同一天同一个人只发一次。
        不补发：如果插件启动时已过了设定时间窗口，当天不再发送。
        """
        now = self._now()
        today_str = now.strftime("%m-%d")           # 如 "07-16"
        today_key = now.strftime("%Y-%m-%d")         # 如 "2026-07-16"

        # 计算当前和设定的「分钟数」（从零点起算），用于窗口判断
        now_minutes = now.hour * 60 + now.minute
        config_minutes = self.birthday_send_hour * 60 + self.birthday_send_minute

        # 2 分钟触发窗口：[config_minutes, config_minutes + 1]
        if now_minutes < config_minutes or now_minutes > config_minutes + 1:
            return

        # 在窗口内，开始逐个检查名单
        for entry in self.birthday_list:
            qq = str(entry.get("qq", "")).strip()
            birthday_raw = str(entry.get("birthday", "")).strip()
            group_id = str(entry.get("group_id", "")).strip()

            # --- 跳过检查 1：字段不完整 ---
            if not qq or not birthday_raw or not group_id:
                logger.info(
                    "[SmartReminder] 生日跳过: qq=%s, birthday=%s, group=%s → 字段不完整",
                    qq, birthday_raw, group_id
                )
                continue

            # --- 跳过检查 2：日期不匹配 ---
            birthday_normalized = self._normalize_date(birthday_raw)
            if birthday_normalized != today_str:
                logger.info(
                    "[SmartReminder] 生日跳过: qq=%s, 生日=%s(→%s), 今日=%s → 日期不匹配",
                    qq, birthday_raw, birthday_normalized, today_str
                )
                continue

            # --- 跳过检查 3：今天已发送过 ---
            sent_key = f"{today_key}_{qq}"
            if self.birthday_sent.get(sent_key):
                logger.info(
                    "[SmartReminder] 生日跳过: qq=%s → 今天(%s)已发送过",
                    qq, today_key
                )
                continue

            # --- 发送祝贺 ---
            logger.info("[SmartReminder] 生日匹配! qq=%s, 群=%s, 开始发送...", qq, group_id)
            await self._send_birthday_message(qq, group_id, sent_key)

    async def _send_birthday_message(self, qq: str, group_id: str, sent_key: str):
        """构建并发送生日祝贺消息，然后记录到 birthday_sent。"""
        # 替换 {qq} 占位符
        msg_text = self.birthday_message.replace("{qq}", qq)

        # 构建 MessageChain：将 [at:xxx] 标签转为 At 组件，其余为 Plain 文本
        msg = MessageChain()
        last_idx = 0
        found_at = False
        for match in AT_TAG_PATTERN.finditer(msg_text):
            found_at = True
            start, end = match.span()
            # 标签前的纯文本
            if start > last_idx:
                msg = msg.message(msg_text[last_idx:start])
            # At 组件
            target_id = match.group(1)
            msg = msg.at(target_id, target_id)
            last_idx = end

        # 标签后的剩余文本
        if last_idx < len(msg_text):
            msg = msg.message(msg_text[last_idx:])
        elif not found_at:
            # 整条消息没有 @ 标签
            msg = msg.message(msg_text)

        # 发送到群（umo 格式: platform:MessageType:sessionId，必须 3 部分）
        umo = f"aiocqhttp:GroupMessage:{group_id}"
        try:
            await self.context.send_message(umo, msg)
            self.birthday_sent[sent_key] = True
            await self._save_birthday_sent()
            logger.info("[SmartReminder] 生日祝贺已发送: qq=%s, 群=%s", qq, group_id)
        except Exception as e:
            logger.error(
                "[SmartReminder] 生日祝贺发送失败: qq=%s, 群=%s, 错误=%s", qq, group_id, e
            )

    # ============================================================
    # 时间解析工具
    # ============================================================

    def _parse_minutes(self, value: str) -> Optional[int]:
        """从字符串中提取分钟数"""
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
        return None

    @staticmethod
    def _normalize_date(date_str: str) -> str:
        """标准化日期格式为 MM-DD（带前导零）。
        支持 "7-16" → "07-16", "07-16" → "07-16", "7月16日" → "07-16"
        """
        match = re.match(r"(\d{1,2})\D+(\d{1,2})", date_str.strip())
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            return f"{month:02d}-{day:02d}"
        return date_str.strip()

    def _parse_absolute_time(self, value: str, now: datetime) -> Optional[datetime]:
        """解析绝对时间字符串，支持多种格式。返回带时区的 datetime。"""
        formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y年%m月%d日 %H:%M",
            "%Y年%m月%d日%H点%M分",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(value.strip(), fmt).replace(tzinfo=self.tz)
                return dt
            except ValueError:
                continue

        # 不含年份的格式，自动补充今年
        yearless_formats = [
            "%m-%d %H:%M",
            "%m月%d日 %H:%M",
            "%m月%d日%H点%M分",
            "%m/%d %H:%M",
        ]
        for fmt in yearless_formats:
            try:
                dt = datetime.strptime(value.strip(), fmt).replace(tzinfo=self.tz)
                dt = dt.replace(year=now.year)
                if dt < now:
                    dt = dt.replace(year=now.year + 1)
                return dt
            except ValueError:
                continue

        return None

    @staticmethod
    def _format_duration(td: timedelta) -> str:
        """将 timedelta 格式化为友好的中文时长"""
        total_seconds = int(td.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}秒"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}分钟"
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if hours < 24:
            if remaining_minutes == 0:
                return f"{hours}小时"
            return f"{hours}小时{remaining_minutes}分钟"
        days = hours // 24
        remaining_hours = hours % 24
        if remaining_hours == 0:
            return f"{days}天"
        return f"{days}天{remaining_hours}小时"
