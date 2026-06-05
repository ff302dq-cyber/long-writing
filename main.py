"""
AstrBot 插件：长文写作助手
使用独立 provider 生成长文章，并在群聊中以合并转发形式发送。
"""

import re
from collections import OrderedDict
from typing import Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 缓存最大条数
MAX_CACHE_SIZE = 100


@register(
    "long_writer",
    "YourName",
    "使用独立写作模型生成长文章，支持 wr+字数 指令和群聊合并转发输出",
    "1.1.0",
    ""
)
class LongWriterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 使用 OrderedDict 做简易 LRU 缓存
        self.article_cache: OrderedDict[str, str] = OrderedDict()
        logger.info("[LongWriter] 长文写作助手已加载")

    # ============================================================
    # 指令解析
    # ============================================================

    def _get_wake_prefixes(self) -> list[str]:
        """读取 AstrBot 配置的唤醒词前缀列表"""
        prefixes = []
        try:
            astrbot_config = self.context.get_config()
            wake_prefix = astrbot_config.get("wake_prefix", None)
            if wake_prefix is None:
                provider_settings = astrbot_config.get("provider_settings", {})
                wake_prefix = provider_settings.get("wake_prefix", "/")

            if isinstance(wake_prefix, list):
                prefixes = [str(p) for p in wake_prefix if str(p)]
            elif isinstance(wake_prefix, str):
                prefixes = [wake_prefix] if wake_prefix else [""]
        except Exception as e:
            logger.warning(f"[LongWriter] 读取唤醒词失败，使用默认 /: {e}")
            prefixes = ["/"]

        prefixes = prefixes or ["/"]
        prefixes.append("")
        return sorted(set(prefixes), key=len, reverse=True)

    def _parse_write_command(self, message: str) -> Optional[Tuple[Optional[int], str]]:
        """
        解析 wr 指令。
        匹配 {唤醒词}wr{可选数字} {需求内容}
        返回 (字数或None, 需求文本) 或 None
        """
        msg = (message or "").strip()
        for prefix in self._get_wake_prefixes():
            if prefix and not msg.startswith(prefix):
                continue
            body = msg[len(prefix):].strip() if prefix else msg
            match = re.match(r"^wr(\d*)\s+(.+)$", body, re.S)
            if not match:
                continue
            word_count = int(match.group(1)) if match.group(1) else None
            requirement = match.group(2).strip()
            if requirement:
                return word_count, requirement
        return None

    def _clamp_word_count(self, word_count: Optional[int]) -> Optional[int]:
        """将字数限制在配置的 min/max 范围内"""
        if word_count is None:
            return None
        min_words = int(self.config.get("min_words", 50))
        max_words = int(self.config.get("max_words", 5000))
        if max_words > 0:
            word_count = min(word_count, max_words)
        if min_words > 0:
            word_count = max(word_count, min_words)
        return word_count

    def _is_continuation_request(self, requirement: str) -> bool:
        keywords = ("续写", "接着写", "继续写", "继续", "往下写", "扩写")
        return any(kw in requirement for kw in keywords)

    def _text_from_component(self, comp) -> str:
        texts = []
        if isinstance(comp, dict):
            for key in ("text", "raw_message"):
                value = comp.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
            data = comp.get("data")
            if isinstance(data, dict):
                value = data.get("text")
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
            content = comp.get("content") or comp.get("message")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            elif isinstance(content, list):
                nested = self._text_from_chain(content)
                if nested:
                    texts.append(nested)
            return "\n".join(texts).strip()

        for key in ("text", "content", "message", "raw_message"):
            value = getattr(comp, key, None)
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
            elif isinstance(value, list):
                nested = self._text_from_chain(value)
                if nested:
                    texts.append(nested)
        return "\n".join(texts).strip()

    def _text_from_chain(self, chain) -> str:
        texts = []
        for comp in chain or []:
            text = self._text_from_component(comp)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()

    # ============================================================
    # 用户信息与缓存
    # ============================================================

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return str(getattr(event.message_obj, "group_id", "") or "")

    def _cache_key(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id())
        group_id = self._get_group_id(event)
        return f"{user_id}_{group_id}" if group_id else f"{user_id}_private"

    async def _get_display_name(self, event: AstrMessageEvent) -> str:
        sender = getattr(event.message_obj, "sender", {}) or {}
        if isinstance(sender, dict):
            card = (sender.get("card") or "").strip()
            if card:
                return card
            nickname = (sender.get("nickname") or "").strip()
            if nickname:
                return nickname

        group_id = self._get_group_id(event)
        if group_id and event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    result = await event.bot.api.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(event.get_sender_id()),
                    )
                    data = result.get("data", result) if isinstance(result, dict) else {}
                    card = (data.get("card") or "").strip()
                    if card:
                        return card
                    nickname = (data.get("nickname") or "").strip()
                    if nickname:
                        return nickname
            except Exception as e:
                logger.warning(f"[LongWriter] 查询群成员信息失败: {e}")

        if isinstance(sender, dict):
            user_id = str(sender.get("user_id", "")).strip()
            if user_id:
                return user_id
        return str(event.get_sender_id())

    def _update_cache(self, event: AstrMessageEvent, article: str, previous_article: str):
        key = self._cache_key(event)
        if previous_article:
            self.article_cache[key] = f"{previous_article.rstrip()}\n\n{article.strip()}"
        else:
            self.article_cache[key] = article.strip()
        # 移到末尾（最近使用）
        self.article_cache.move_to_end(key)
        # 淘汰最旧的
        while len(self.article_cache) > MAX_CACHE_SIZE:
            self.article_cache.popitem(last=False)

    # ============================================================
    # 人设读取
    # ============================================================

    async def _get_persona_prompt(self, event: AstrMessageEvent) -> str:
        """通过 PersonaManager 读取当前会话绑定的人设卡 system_prompt"""
        try:
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_default_persona_v3(umo=event.unified_msg_origin)
            # get_default_persona_v3 返回 Personality (TypedDict)，取 "prompt" 字段
            prompt = str(persona.get("prompt", "") or "").strip()
            if prompt:
                logger.info("[LongWriter] 已读取当前会话人设卡")
            return prompt
        except Exception as e:
            logger.warning(f"[LongWriter] 读取人设卡失败: {e}")
            return ""

    async def _build_system_prompt(self, event: AstrMessageEvent) -> str:
        parts = []
        persona_prompt = await self._get_persona_prompt(event)
        if persona_prompt:
            parts.append(persona_prompt)
        writer_prompt = str(self.config.get("writer_system_prompt", "") or "").strip()
        if writer_prompt:
            parts.append(writer_prompt)
        return "\n\n".join(parts).strip()

    # ============================================================
    # 续写：读取前文
    # ============================================================

    def _get_reply_message_id(self, event: AstrMessageEvent):
        """
        从消息链中提取引用消息的 message_id。
        AstrBot 将引用消息解析为 Comp.Reply 组件。
        """
        chain = getattr(event.message_obj, "message", None) or []
        for comp in chain:
            if isinstance(comp, Comp.Reply):
                return getattr(comp, "id", None)
        return None

    def _get_aiocqhttp_bot(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return None
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if isinstance(event, AiocqhttpMessageEvent):
                return event.bot
        except Exception as e:
            logger.warning(f"[LongWriter] 读取 aiocqhttp 事件失败: {e}")
        return None

    def _normalize_action_data(self, result):
        if not isinstance(result, dict):
            return {}
        data = result.get("data", result)
        return data if isinstance(data, dict) else {}

    def _extract_forward_id(self, item) -> Optional[str]:
        if isinstance(item, dict):
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            for key in ("id", "message_id", "res_id", "file"):
                value = data.get(key) or item.get(key)
                if value:
                    return str(value)
            return None

        for key in ("id", "message_id", "res_id"):
            value = getattr(item, key, None)
            if value:
                return str(value)
        return None

    def _is_forward_component(self, item) -> bool:
        if isinstance(item, dict):
            return str(item.get("type", "")).lower() == "forward"
        return type(item).__name__.lower() in ("forward", "forwardmessage")

    def _join_distinct_texts(self, *texts: str) -> str:
        result = []
        for text in texts:
            text = (text or "").strip()
            if not text:
                continue
            if any(text == old or text in old for old in result):
                continue
            result = [old for old in result if old not in text]
            result.append(text)
        return "\n".join(result).strip()

    async def _text_from_component_with_forward(self, event: AstrMessageEvent, comp) -> str:
        if self._is_forward_component(comp):
            forward_id = self._extract_forward_id(comp)
            direct_content = ""
            if isinstance(comp, dict):
                data = comp.get("data") if isinstance(comp.get("data"), dict) else {}
                direct_content = await self._text_from_possible_content(
                    event, data.get("content") or data.get("message")
                )
            if direct_content:
                return direct_content
            if forward_id:
                return await self._get_forward_text_via_api(event, forward_id)

        if isinstance(comp, dict):
            data = comp.get("data") if isinstance(comp.get("data"), dict) else {}
            nested = data.get("content") or data.get("message") or comp.get("content") or comp.get("message")
            nested_text = await self._text_from_possible_content(event, nested)
            plain_text = self._text_from_component(comp)
            return self._join_distinct_texts(plain_text, nested_text)

        nested = getattr(comp, "content", None) or getattr(comp, "message", None)
        nested_text = await self._text_from_possible_content(event, nested)
        plain_text = self._text_from_component(comp)
        return self._join_distinct_texts(plain_text, nested_text)

    async def _text_from_possible_content(self, event: AstrMessageEvent, content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return await self._text_from_chain_with_forward(event, content)
        return ""

    async def _text_from_chain_with_forward(self, event: AstrMessageEvent, chain) -> str:
        texts = []
        for comp in chain or []:
            text = await self._text_from_component_with_forward(event, comp)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()

    async def _get_forward_text_via_api(self, event: AstrMessageEvent, forward_id) -> str:
        """
        读取合并转发正文。不同 OneBot 适配器参数名不完全一致，
        所以依次尝试 id 与 message_id。
        """
        if not forward_id:
            return ""
        bot = self._get_aiocqhttp_bot(event)
        if not bot:
            return ""

        last_error = None
        for params in ({"id": str(forward_id)}, {"message_id": str(forward_id)}):
            try:
                result = await bot.api.call_action("get_forward_msg", **params)
                data = self._normalize_action_data(result)
                messages = data.get("messages") or data.get("message") or []
                if isinstance(messages, dict):
                    messages = [messages]
                text = await self._text_from_chain_with_forward(event, messages)
                if text:
                    return text
            except Exception as e:
                last_error = e

        if last_error:
            logger.warning(f"[LongWriter] get_forward_msg 调用失败: {last_error}")
        return ""

    async def _get_reply_text_via_api(self, event: AstrMessageEvent, message_id) -> str:
        """先读取被引用消息，再从其中提取普通文本或合并转发内容。"""
        if not message_id:
            return ""
        bot = self._get_aiocqhttp_bot(event)
        if not bot:
            return ""

        try:
            result = await bot.api.call_action("get_msg", message_id=int(message_id))
            data = self._normalize_action_data(result)
            message = data.get("message") or data.get("content") or ""
            raw_message = data.get("raw_message") or ""

            text = await self._text_from_possible_content(event, message)
            if text:
                return text
            if isinstance(raw_message, str) and raw_message.strip():
                return raw_message.strip()
        except Exception as e:
            logger.warning(f"[LongWriter] get_msg 读取引用消息失败: {e}")
        return ""

    async def _get_previous_article(self, event: AstrMessageEvent, requirement: str) -> str:
        """获取续写所需的前文：优先内存缓存，兜底 get_forward_msg"""
        if not self._is_continuation_request(requirement):
            return ""

        key = self._cache_key(event)
        cached = self.article_cache.get(key, "")
        if cached:
            logger.info("[LongWriter] 从内存缓存读取前文用于续写")
            return cached

        # 缓存没命中，尝试读取被引用消息；如果引用的是合并转发，会继续拆 get_forward_msg。
        reply_msg_id = self._get_reply_message_id(event)
        if reply_msg_id:
            quoted_text = await self._get_reply_text_via_api(event, reply_msg_id)
            if quoted_text:
                logger.info("[LongWriter] 通过引用消息读取前文用于续写")
                return quoted_text

        logger.warning("[LongWriter] 续写请求但未找到前文（缓存为空且无引用消息）")
        return ""

    # ============================================================
    # 写作核心
    # ============================================================

    def _build_user_prompt(self, word_count: Optional[int], requirement: str, previous_article: str = "") -> str:
        if word_count is not None:
            word_line = (
                f"【重要】目标字数：最终正文应尽量接近 {word_count} 个中文字符，"
                f"允许上下浮动 50 字。也就是说正文长度应控制在 "
                f"{max(word_count - 50, 1)} 到 {word_count + 50} 个中文字符之间。"
                f"不要明显少写，也不要明显超写。\n"
            )
        else:
            word_line = "目标字数：用户没有指定，请根据题材和需求自由决定合适篇幅。\n"

        previous_block = ""
        if previous_article:
            previous_block = (
                f"\n【前文】\n{previous_article}\n\n"
                f"用户正在要求续写、扩写或接着写。请以前文为依据，不要重复前文，只输出新的正文。\n"
            )

        return (
            f"这是长文写作任务，不是普通聊天回复。请按下面要求写一篇完整中文文章。\n"
            f"必须合理换行分段、语言流畅、正确使用中文标点符号。\n"
            f"{word_line}"
            f"详细需求：{requirement}\n"
            f"{previous_block}\n"
            f"如果当前对话中包含用户引用的消息、历史文章或上文，并且需求是续写、扩写或接着写，"
            f"请以上下文为依据，不要重复前文，只输出新的正文。\n"
            f'只输出文章正文。不要输出收件人称呼，不要输出"给某某的文章"，不要解释写作过程。'
            + (
                f"\n再次提醒：正文请控制在 {max(word_count - 50, 1)} 到 {word_count + 50} 个中文字符之间。"
                if word_count else ""
            )
        )

    async def _generate_article(
        self,
        event: AstrMessageEvent,
        word_count: Optional[int],
        requirement: str,
        previous_article: str,
    ) -> str:
        provider_id = str(self.config.get("writer_provider_id", "") or "").strip()
        if not provider_id:
            raise ValueError("还没有在插件配置里选择长文写作模型提供商。")

        # 输出写作模型信息，方便确认走的是贵模型
        try:
            prov = self.context.get_provider_by_id(provider_id)
            if prov:
                meta = prov.meta()
                model_config = getattr(meta, "model_config", None)
                model_name = getattr(model_config, "model", None) or getattr(meta, "model", None) or "unknown"
                logger.info(f"[LongWriter] 写作模型: provider_id={provider_id}, model={model_name}")
            else:
                logger.info(f"[LongWriter] 写作模型: provider_id={provider_id}")
        except Exception:
            logger.info(f"[LongWriter] 写作模型: provider_id={provider_id}")

        system_prompt = await self._build_system_prompt(event)

        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=self._build_user_prompt(word_count, requirement, previous_article),
            system_prompt=system_prompt,
        )
        return str(getattr(response, "completion_text", "") or "").strip()

    # ============================================================
    # 输出格式与发送
    # ============================================================

    async def _build_article_text(self, event: AstrMessageEvent, article: str) -> str:
        name = await self._get_display_name(event)
        return f"——给【{name}】的文章——\n{article.strip()}"

    def _build_forward_chain(self, event: AstrMessageEvent, text: str):
        """
        构建合并转发消息链。
        参考 AstrBot 官方文档：yield event.chain_result([node])
        """
        bot_uin = str(getattr(event.message_obj, "self_id", "") or "0")
        node_name = str(self.config.get("forward_node_name", "长文写作助手")).strip() or "长文写作助手"
        node = Comp.Node(
            uin=bot_uin,
            name=node_name,
            content=[Comp.Plain(text=text)],
        )
        return event.chain_result([node])

    # ============================================================
    # 主入口
    # ============================================================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        parsed = self._parse_write_command(event.message_str)
        if not parsed:
            return

        event.stop_event()
        word_count, requirement = parsed
        word_count = self._clamp_word_count(word_count)

        try:
            previous_article = await self._get_previous_article(event, requirement)
            article = await self._generate_article(event, word_count, requirement, previous_article)
        except Exception as e:
            logger.warning(f"[LongWriter] 长文生成失败: {type(e).__name__}: {e}", exc_info=True)
            yield event.plain_result(f"长文生成失败：{e}")
            return

        if not article:
            yield event.plain_result("长文生成失败：模型没有返回正文。")
            return

        if word_count and not (max(word_count - 50, 1) <= len(article) <= word_count + 50):
            logger.warning(
                f"[LongWriter] 模型输出 {len(article)} 字，超出目标 {word_count}±50 字范围"
            )

        self._update_cache(event, article, previous_article)
        final_text = await self._build_article_text(event, article)

        if self._get_group_id(event) and bool(self.config.get("always_forward_in_group", True)):
            yield self._build_forward_chain(event, final_text)
            return

        yield event.chain_result([Comp.Plain(text=final_text)])

    async def terminate(self):
        logger.info("[LongWriter] 长文写作助手已卸载")
