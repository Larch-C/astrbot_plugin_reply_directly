import asyncio
import json
import re
from collections import defaultdict
from asyncio import Lock

from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.api.provider import Personality


@register(
    "astrbot_plugin_reply_directly",
    "qa296",
    "让您的 AstrBot 在群聊中变得更加生动和智能！本插件使其可以主动的连续交互，并完全遵循您设定的人格。",
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
        logger.info("ReplyDirectly插件 v1.3.0 加载成功！现已支持人格（Persona）继承。")
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

    async def _get_system_prompt_for_umo(self, umo: str) -> str:
        """根据 unified_msg_origin 获取当前会话生效的 System Prompt。"""
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not curr_cid:
                return ""

            conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
            if not conversation:
                return ""

            persona_id = conversation.persona_id
            # 如果没有特定persona_id且不为显式取消，则使用默认
            if not persona_id and persona_id != "[%None]":
                persona_id = self.context.provider_manager.selected_default_persona.get("name")
            
            if not persona_id or persona_id == "[%None]":
                return ""

            all_personas: list[Personality] = self.context.provider_manager.personas
            for persona in all_personas:
                if persona.name == persona_id:
                    logger.debug(f"为会话 {umo} 找到生效的人格: {persona.name}")
                    return persona.prompt
            
            logger.warning(f"为会话 {umo} 找到了persona_id '{persona_id}'，但未在已加载人格中找到匹配项。")
            return ""

        except Exception as e:
            logger.error(f"获取System Prompt时出错: {e}", exc_info=True)
            return ""


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

            async with self.immersive_lock:
                self.direct_reply_context[group_id] = {"cid": curr_cid, "context": context, "umo": uid}
            logger.info(f"[沉浸式对话] 已为群 {group_id} 开启单次直接回复模式，并保存了当前对话上下文。")
        except Exception as e:
            logger.error(f"[沉浸式对话] 保存上下文时出错: {e}", exc_info=True)

    # -----------------------------------------------------
    # Feature 2: 主动插话 (Proactive Interjection)
    # -----------------------------------------------------

    async def _start_proactive_check(self, group_id: str, unified_msg_origin: str):
        """辅助函数，用于启动或重置一个群组的主动插话检查任务。"""
        async with self.group_task_lock:
            if group_id in self.active_timers:
                self.active_timers[group_id].cancel()
            self.group_chat_buffer[group_id].clear()
            task = asyncio.create_task(self._proactive_check_task(group_id, unified_msg_origin))
            self.active_timers[group_id] = task
        logger.info(f"[主动插话] 已为群 {group_id} 启动/重置了延时检查任务。")

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        if not self.config.get("enable_plugin", True) or not self.config.get("enable_proactive_reply", True): return
        if event.is_private_chat(): return
        group_id = event.get_group_id()
        if not group_id: return
        await self._start_proactive_check(group_id, event.unified_msg_origin)

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        try:
            delay = self.config.get("proactive_reply_delay", 8)
            await asyncio.sleep(delay)

            chat_history = []
            async with self.group_task_lock:
                if self.active_timers.get(group_id) is not asyncio.current_task(): return
                if group_id in self.group_chat_buffer:
                    chat_history = self.group_chat_buffer.pop(group_id, [])

            if not chat_history: return

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

            # 【核心修改】获取并使用 System Prompt
            system_prompt = await self._get_system_prompt_for_umo(unified_msg_origin)
            
            llm_response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
            json_string = self._extract_json_from_text(llm_response.completion_text)
            if not json_string: return

            try:
                decision_data = json.loads(json_string)
                if decision_data.get("should_reply") and decision_data.get("content"):
                    content = decision_data["content"]
                    logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                    message_chain = MessageChain().message(content)
                    await self.context.send_message(unified_msg_origin, message_chain)
                    logger.info(f"[主动插话] 插话成功，为群 {group_id} 重新启动检测。")
                    await self._start_proactive_check(group_id, unified_msg_origin)
            except (json.JSONDecodeError, TypeError, AttributeError): pass

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
        """统一处理所有群聊消息"""
        if not self.config.get("enable_plugin", True): return
        group_id = event.get_group_id()
        if not group_id or event.get_sender_id() == event.get_self_id(): return

        # 逻辑1: 检查是否处于沉浸式对话模式
        if self.config.get("enable_immersive_chat", True):
            saved_data = None
            async with self.immersive_lock:
                if group_id in self.direct_reply_context:
                    saved_data = self.direct_reply_context.pop(group_id)
            if saved_data:
                logger.info(f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，将携带上下文和人格触发LLM。")
                
                # 【核心修改】获取并使用 System Prompt
                system_prompt = await self._get_system_prompt_for_umo(saved_data["umo"])
                
                event.stop_event()
                yield event.request_llm(
                    prompt=event.message_str,
                    contexts=saved_data.get("context", []),
                    session_id=saved_data.get("cid"),
                    system_prompt=system_prompt
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
            for task in self.active_timers.values(): task.cancel()
            self.active_timers.clear()
            self.group_chat_buffer.clear()
        async with self.immersive_lock:
            self.direct_reply_context.clear()
        logger.info("ReplyDirectly插件所有后台任务已清理。")
