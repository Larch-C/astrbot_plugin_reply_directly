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


@register(
    "astrbot_plugin_reply_directly",
    "qa296",
    "让您的 AstrBot 在群聊中变得更加生动和智能！本插件使其可以主动的连续交互。",
    "1.4.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.config = config
        # 使用独立的锁，避免逻辑冲突
        self.immersive_lock = Lock()
        self.proactive_lock = Lock()

        # 新的数据结构来存储沉浸式会话
        # key: (group_id, user_id)
        # value: {'context': [], 'timer': asyncio.TimerHandle}
        self.immersive_sessions = {}

        self.active_proactive_timers = {}
        self.group_chat_buffer = defaultdict(list)
        logger.info("ReplyDirectly插件 v1.4.0 加载成功！")
        logger.debug(f"插件配置: {self.config}")

    def _extract_json_from_text(self, text: str) -> str:
        # 优先策略：寻找被 ```json ... ``` 包裹的代码块
        json_block_pattern = r'```json\s*(\{.*?\})\s*```'
        match = re.search(json_block_pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 备用策略：寻找第一个 { 和最后一个 } 之间的所有内容
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1].strip()
            
        return ""

    # -----------------------------------------------------
    # 辅助函数
    # -----------------------------------------------------

    async def _arm_immersive_session(self, event: AstrMessageEvent):
        """当机器人回复后，为目标用户启动一个限时的沉浸式会话。"""
        if not self.config.get("enable_immersive_chat", True):
            return

        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if not group_id or not user_id:
            return

        session_key = (group_id, user_id)
        
        async with self.immersive_lock:
            if session_key in self.immersive_sessions:
                self.immersive_sessions[session_key]['timer'].cancel()

            context = []
            try:
                uid = event.unified_msg_origin
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
                    if conversation and conversation.history:
                        context = json.loads(conversation.history)
            except Exception as e:
                logger.error(f"[沉浸式对话] 准备上下文时出错: {e}", exc_info=True)

            timeout = self.config.get("immersive_reply_timeout", 120)
            timer = asyncio.get_running_loop().call_later(
                timeout, self._clear_immersive_session, session_key
            )
            
            self.immersive_sessions[session_key] = {
                'context': context,
                'timer': timer
            }
            logger.info(f"[沉浸式对话] 已为群 {group_id} 的用户 {user_id} 开启了 {timeout}s 的沉浸式会话。")

    def _clear_immersive_session(self, session_key):
        """超时后清理沉浸式会话的回调函数"""
        if session_key in self.immersive_sessions:
            self.immersive_sessions.pop(session_key, None)
            logger.debug(f"[沉浸式对话] 会话 {session_key} 已超时并清理。")

    async def _start_proactive_check(self, group_id: str, unified_msg_origin: str):
        """辅助函数，用于启动或重置一个群组的主动插话检查任务。"""
        async with self.proactive_lock:
            if group_id in self.active_proactive_timers:
                self.active_proactive_timers[group_id].cancel()
                logger.debug(f"[主动插话] 取消了群 {group_id} 的旧计时器。")

            self.group_chat_buffer[group_id].clear()
            task = asyncio.create_task(
                self._proactive_check_task(group_id, unified_msg_origin)
            )
            self.active_proactive_timers[group_id] = task
        logger.debug(f"[主动插话] 已为群 {group_id} 启动/重置了延时检查任务。")

    # -----------------------------------------------------
    # 核心任务与钩子
    # -----------------------------------------------------

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        """机器人发送消息后，同时启动主动插话和沉浸式对话的计时器。"""
        if not self.config.get("enable_plugin", True):
            return
        if event.is_private_chat():
            return

        # 1. 启动/重置主动插话任务 (针对整个群聊)
        if self.config.get("enable_proactive_reply", True):
            await self._start_proactive_check(event.get_group_id(), event.unified_msg_origin)
        
        # 2. 启动/重置沉浸式对话任务 (针对被回复的那个用户)
        await self._arm_immersive_session(event)

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        """延时任务，在指定时间后检查一次是否需要主动插话。"""
        try:
            delay = self.config.get("proactive_reply_delay", 8)
            await asyncio.sleep(delay)

            chat_history = []
            async with self.proactive_lock:
                if self.active_proactive_timers.get(group_id) is not asyncio.current_task():
                    return
                if group_id in self.group_chat_buffer:
                    chat_history = self.group_chat_buffer.pop(group_id, [])

            if not chat_history:
                logger.debug(f"[主动插话] 群 {group_id} 在 {delay}s 内无新消息，任务结束。")
                return

            logger.debug(f"[主动插话] 群 {group_id} 计时结束，收集到 {len(chat_history)} 条消息，请求LLM判断。")

            formatted_history = "\n".join(chat_history)
            user_prompt = f"--- 对话记录 ---\n{formatted_history}\n--- 对话记录结束 ---"
            instruction = (
                "在一个群聊里，在你刚刚说完话之后的一段时间里，群里发生了以下的对话。根据以上对话内容，你是否应该主动插话，"
                "无论如何请严格按照JSON格式在```json ... ```代码块中回答。"
                f'格式示例：\n```json\n{{"should_reply": true, "content": "你的回复内容"}}\n```\n'
                f'或\n```json\n{{"should_reply": false, "content": ""}}\n```'
            )

            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return

            llm_response = await provider.text_chat(prompt=user_prompt, system_prompt=instruction)
            json_string = self._extract_json_from_text(llm_response.completion_text)
            if not json_string:
                logger.warning(f"[主动插话] 从LLM回复中未能提取出JSON。原始回复: {llm_response.completion_text}")
                return

            try:
                decision_data = json.loads(json_string)
                if decision_data.get("should_reply") and decision_data.get("content"):
                    content = decision_data["content"]
                    logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                    message_chain = MessageChain().message(content)
                    await self.context.send_message(unified_msg_origin, message_chain)
                else:
                    logger.info("[主动插话] LLM判断无需回复。")
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                logger.error(f"[主动插话] 解析LLM的JSON回复失败: {e}\n原始回复: {llm_response.completion_text}\n清理后文本: '{json_string}'")

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的检查任务被取消。")
        except Exception as e:
            logger.error(f"[主动插话] 群 {group_id} 的检查任务出现未知异常: {e}", exc_info=True)
        finally:
            async with self.proactive_lock:
                if self.active_proactive_timers.get(group_id) is asyncio.current_task():
                    self.active_proactive_timers.pop(group_id, None)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """统一处理所有群聊消息，优先处理沉浸式对话。"""
        if not self.config.get("enable_plugin", True):
            return

        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if not group_id or sender_id == event.get_self_id():
            return

        # --- 逻辑1: 检查是否触发了沉浸式对话 ---
        session_key = (group_id, sender_id)
        session_data = None
        
        async with self.immersive_lock:
            if session_key in self.immersive_sessions:
                if any(isinstance(comp, Comp.At) for comp in event.message_obj.message):
                    logger.info(f"[沉浸式对话] 用户 {sender_id} @了别人，沉浸式会话失效。")
                    self.immersive_sessions[session_key]['timer'].cancel()
                    self.immersive_sessions.pop(session_key, None)
                else:
                    session_data = self.immersive_sessions.pop(session_key)
                    session_data['timer'].cancel()

        if session_data:
            logger.info(f"[沉浸式对话] 捕获到用户 {sender_id} 的连续消息，开始判断是否回复。")
            
            async with self.proactive_lock:
                if group_id in self.active_proactive_timers:
                    self.active_proactive_timers[group_id].cancel()
                    logger.debug(f"[沉浸式对话] 已取消群 {group_id} 的主动插话任务。")
            
            event.stop_event()

            saved_context = session_data.get('context', [])
            user_prompt = event.message_str
            instruction = (
                "你刚刚和一位用户进行了对话。现在，这位用户紧接着发送了以下新消息。请根据你们之前的对话上下文和这条新消息，判断你是否应该跟进回复。"
                "无论如何请严格按照JSON格式在```json ... ```代码块中回答。"
                f'格式示例：\n```json\n{{"should_reply": true, "content": "你的回复内容"}}\n```\n'
                f'或\n```json\n{{"should_reply": false, "content": ""}}\n```'
            )

            provider = self.context.get_using_provider()
            if not provider:
                return

            llm_response = await provider.text_chat(
                prompt=user_prompt, 
                contexts=saved_context, 
                system_prompt=instruction
            )
            
            json_string = self._extract_json_from_text(llm_response.completion_text)
            if not json_string:
                logger.warning(f"[沉浸式对话] 从LLM回复中未能提取出JSON。原始回复: {llm_response.completion_text}")
                return

            try:
                decision_data = json.loads(json_string)
                if decision_data.get("should_reply") and decision_data.get("content"):
                    content = decision_data["content"]
                    logger.info(f"[沉浸式对话] LLM判断需要回复，内容: {content[:50]}...")
                    yield event.plain_result(content)
                else:
                    logger.info("[沉浸式对话] LLM判断无需回复。")
            except Exception as e:
                logger.error(f"[沉浸式对话] 解析或处理LLM的JSON时出错: {e}")
            
            return

        # --- 逻辑2: 如果没有触发沉浸式对话，则为主动插话功能缓冲消息 ---
        if self.config.get("enable_proactive_reply", True):
            async with self.proactive_lock:
                if group_id in self.active_proactive_timers:
                    sender_name = event.get_sender_name() or sender_id
                    message_text = event.message_str.strip()
                    if message_text and len(self.group_chat_buffer[group_id]) < 20:
                        self.group_chat_buffer[group_id].append(
                            f"{sender_name}: {message_text}"
                        )

    async def terminate(self):
        """插件被卸载/停用时调用，用于清理"""
        logger.info("正在卸载ReplyDirectly插件，取消所有后台任务...")
        
        async with self.proactive_lock:
            for task in self.active_proactive_timers.values():
                task.cancel()
            self.active_proactive_timers.clear()
            self.group_chat_buffer.clear()

        async with self.immersive_lock:
            for session in self.immersive_sessions.values():
                session['timer'].cancel()
            self.immersive_sessions.clear()

        logger.info("ReplyDirectly插件所有后台任务已清理。")
