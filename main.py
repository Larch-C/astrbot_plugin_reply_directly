import asyncio
import json
import re
from collections import defaultdict
from asyncio import Lock

from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import Personality


@register(
    "astrbot_plugin_reply_directly",
    "qa296",
    "让您的 AstrBot 在群聊中变得更加生动和智能！本插件使其可以主动的连续交互。",
    "1.3.0", 
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.config = config
        self.immersive_lock = Lock()
        self.group_task_lock = Lock()
        self.direct_reply_context = {}
        self.active_timers = {}
        self.group_chat_buffer = defaultdict(list)
        logger.info("ReplyDirectly加载成功")
        logger.debug(f"插件配置: {self.config}")

    def _extract_json_from_text(self, text: str) -> str:
        pattern = r"```json\s*(.*?)\s*```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1].strip()
        return text.strip()

    # ==================================================================
    # [FIXED HELPER FUNCTION] - Corrected version
    # ==================================================================
    async def _get_active_persona_prompt(self, unified_msg_origin: str) -> str:
        """
        获取指定会话当前正在使用的人格(System Prompt)。
        会处理默认人格、指定人格和无任何人格的情况。
        """
        try:
            conv_mgr = self.context.conversation_manager
            provider_mgr = self.context.provider_manager

            curr_cid = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
            if not curr_cid:
                # 如果没有会话，则使用默认人格
                persona_name = provider_mgr.selected_default_persona.get("name")
            else:
                conversation = await conv_mgr.get_conversation(unified_msg_origin, curr_cid)
                persona_id = conversation.persona_id if conversation else None

                if persona_id == "[%None]":
                    return ""  # 用户显式取消了人格
                
                persona_name = persona_id # 如果有ID，就用ID去找
                if not persona_name: # 如果ID是None，就用默认的
                    persona_name = provider_mgr.selected_default_persona.get("name")

            if not persona_name:
                return "" # 连默认人格都没有

            # 从加载的所有人格中找到匹配的
            # [FIX] The root cause of the error is here. `personas` is a list of dicts.
            all_personas: list[dict] = provider_mgr.personas
            for p in all_personas:
                # [FIX] Use dictionary access .get('name') instead of attribute access .name
                if p.get("name") == persona_name:
                    logger.debug(f"为会话 {unified_msg_origin} 找到人格: {p.get('name')}")
                    # [FIX] Use dictionary access .get('prompt') to return the prompt
                    return p.get("prompt", "")
            
            logger.warning(f"会话 {unified_msg_origin} 指定的人格 '{persona_name}' 未找到，将不使用人格。")
            return ""

        except Exception as e:
            logger.error(f"获取人格时发生错误: {e}", exc_info=True)
            return "" # 出错时返回空字符串，保证安全

    # -----------------------------------------------------
    # Feature 1: 沉浸式对话 (Immersive Chat)
    # -----------------------------------------------------

    @filter.llm_tool()
    async def enable_direct_reply_once(self, event: AstrMessageEvent):
        """
        当LLM认为可以开启沉浸式对话时调用此函数。这会让机器人在该群组的下一条消息时直接回复，无需@。此效果仅生效一次。
        """
        if not self.config.get("enable_immersive_chat", True):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        try:
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                logger.warning(f"[沉浸式对话] 无法获取群 {group_id} 的当前会话ID，无法保存上下文。")
                return

            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            context = json.loads(conversation.history) if conversation and conversation.history else []
            
            # [MODIFIED] 获取并保存当前的人格
            system_prompt = await self._get_active_persona_prompt(uid)

            async with self.immersive_lock:
                self.direct_reply_context[group_id] = {
                    "cid": curr_cid,
                    "context": context,
                    "system_prompt": system_prompt, # 保存人格
                }
            logger.info(f"[沉浸式对话] 已为群 {group_id} 开启单次直接回复模式，并保存了当前对话上下文及人格。")
        except Exception as e:
            logger.error(f"[沉浸式对话] 保存上下文时出错: {e}", exc_info=True)

    # -----------------------------------------------------
    # Feature 2: 主动插话 (Proactive Interjection)
    # -----------------------------------------------------

    async def _start_proactive_check(self, group_id: str, unified_msg_origin: str):
        async with self.group_task_lock:
            if group_id in self.active_timers:
                self.active_timers[group_id].cancel()
            self.group_chat_buffer[group_id].clear()
            task = asyncio.create_task(self._proactive_check_task(group_id, unified_msg_origin))
            self.active_timers[group_id] = task
        logger.info(f"[主动插话] 已为群 {group_id} 启动/重置了延时检查任务。")

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin", True) or not self.config.get("enable_proactive_reply", True):
            return
        if event.is_private_chat():
            return
        group_id = event.get_group_id()
        if not group_id:
            return
        await self._start_proactive_check(group_id, event.unified_msg_origin)

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        try:
            delay = self.config.get("proactive_reply_delay", 8)
            await asyncio.sleep(delay)

            chat_history = []
            async with self.group_task_lock:
                if self.active_timers.get(group_id) is not asyncio.current_task():
                    return
                if group_id in self.group_chat_buffer:
                    chat_history = self.group_chat_buffer.pop(group_id, [])

            if not chat_history:
                return

            logger.info(f"[主动插话] 群 {group_id} 计时结束，收集到 {len(chat_history)} 条消息，请求LLM判断。")

            formatted_history = "\n".join(chat_history)
            prompt = (
                f"你是一个名为AstrBot的AI助手。在一个群聊里，在你刚刚说完话之后的一段时间里，群里发生了以下的对话：\n"
                f"--- 对话记录 ---\n{formatted_history}\n--- 对话记录结束 ---\n"
                f"现在请你判断，根据以上对话内容，你是否应该主动插话，以使对话更流畅或提供帮助。请严格按照JSON格式在```json ... ```代码块中回答，不要有任何其他说明文字。\n"
                f'格式示例：\n```json\n{{"should_reply": true, "content": "你的回复内容"}}\n```\n'
                f'或\n```json\n{{"should_reply": false, "content": ""}}\n```'
            )

            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return

            # [MODIFIED] 获取当前会话的人格
            system_prompt = await self._get_active_persona_prompt(unified_msg_origin)

            # [MODIFIED] 将人格传递给LLM
            llm_response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
            json_string = self._extract_json_from_text(llm_response.completion_text)
            if not json_string:
                return

            try:
                decision_data = json.loads(json_string)
                if decision_data.get("should_reply") and decision_data.get("content"):
                    content = decision_data["content"]
                    logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                    message_chain = MessageChain().message(content)
                    await self.context.send_message(unified_msg_origin, message_chain)
                    await self._start_proactive_check(group_id, unified_msg_origin)
                else:
                    logger.info("[主动插话] LLM判断无需回复。")
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                logger.error(f"[主动插话] 解析LLM的JSON回复失败: {e}")

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的检查任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 群 {group_id} 的检查任务出现未知异常: {e}", exc_info=True)
        finally:
            async with self.group_task_lock:
                if self.active_timers.get(group_id) is asyncio.current_task():
                    self.active_timers.pop(group_id, None)

    # -----------------------------------------------------
    # 统一的消息监听器
    # -----------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin", True):
            return

        group_id = event.get_group_id()
        if not group_id or event.get_sender_id() == event.get_self_id():
            return

        # 逻辑1: 检查是否处于沉浸式对话模式
        if self.config.get("enable_immersive_chat", True):
            saved_data = None
            async with self.immersive_lock:
                if group_id in self.direct_reply_context:
                    saved_data = self.direct_reply_context.pop(group_id)

            if saved_data:
                logger.info(f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，将携带上下文和人格触发LLM。")
                saved_cid = saved_data.get("cid")
                saved_context = saved_data.get("context", [])
                # [MODIFIED] 获取保存的人格
                saved_system_prompt = saved_data.get("system_prompt", "")
                
                event.stop_event()
                # [MODIFIED] 将人格传递给LLM
                yield event.request_llm(
                    prompt=event.message_str,
                    contexts=saved_context,
                    session_id=saved_cid,
                    system_prompt=saved_system_prompt,
                )
                return

        # 逻辑2: 为主动插话功能提供支持
        if self.config.get("enable_proactive_reply", True):
            async with self.group_task_lock:
                if group_id in self.active_timers:
                    sender_name = event.get_sender_name() or event.get_sender_id()
                    message_text = event.message_str.strip()
                    if message_text and len(self.group_chat_buffer[group_id]) < 20:
                        self.group_chat_buffer[group_id].append(f"{sender_name}: {message_text}")

    async def terminate(self):
        logger.info("正在卸载ReplyDirectly插件，取消所有后台任务...")
        async with self.group_task_lock:
            for task in self.active_timers.values():
                task.cancel()
            self.active_timers.clear()
            self.group_chat_buffer.clear()
        async with self.immersive_lock:
            self.direct_reply_context.clear()
        logger.info("ReplyDirectly插件所有后台任务已清理。")
