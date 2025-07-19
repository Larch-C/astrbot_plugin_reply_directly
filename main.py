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
    "1.4.1",
    "https://github.com/qa296/astrbot_plugin_reply_directly",
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.config = config
        self.immersive_lock = Lock()
        self.group_task_lock = Lock()
        self.immersive_sessions = {} 
        self.active_timers = {}
        self.group_chat_buffer = defaultdict(list)
        logger.info("ReplyDirectly插件 v1.4.1 加载成功！")
        logger.debug(f"插件配置: {self.config}")

    def _extract_json_from_text(self, text: str) -> str:
        # 优先策略：寻找被 ```json ... ``` 包裹的代码块，这是最明确的格式
        json_block_pattern = r'```json\s*(\{.*?\})\s*```'
        match = re.search(json_block_pattern, text, re.DOTALL)
        if match:
            # group(1) 捕获的是括号内的内容，也就是完整的 { ... }
            return match.group(1).strip()

        # 备用策略：如果上面没找到，就寻找第一个 { 和最后一个 } 之间的所有内容
        # 这比之前的正则表达式更稳定，能处理多行和嵌套
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1].strip()
            
        # 如果最终什么都没找到，返回空字符串，避免后续代码出错
        return ""

    # -----------------------------------------------------
    # Feature 1: 沉浸式对话 (Immersive Chat)
    # -----------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """统一处理所有群聊消息，优先处理沉浸式对话。"""
        if not self.config.get("enable_plugin", True):
            return
    
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if not group_id or sender_id == event.get_self_id():
            return
    
        # 逻辑1: 检查是否满足沉浸式对话激活条件
        if self.config.get("enable_immersive_chat", True):
            session_data = None
            is_valid_session = False
            
            async with self.immersive_lock:
                if group_id in self.immersive_sessions:
                    session_data = self.immersive_sessions[group_id]
                    # 检查是否是目标用户
                    if session_data.get("user_id") == sender_id:
                        is_valid_session = True
    
            if is_valid_session:
                # 检查消息是否包含@
                has_at_mention = any(isinstance(comp, Comp.At) for comp in event.message_obj.message)
                
                if not has_at_mention:
                    logger.info(f"[沉浸式对话] 检测到群 {group_id} 用户 {sender_id} 的沉浸式回复。")
                    
                    # 1. 从状态中取出数据并清理
                    async with self.immersive_lock:
                        active_session = self.immersive_sessions.pop(group_id)
                    
                    # 2. 取消超时任务
                    active_session["task"].cancel()
                    
                    # 3. 停止事件传播，防止触发主动插话和默认LLM行为
                    event.stop_event()
                    
                    # 4. 携带上下文请求LLM
                    yield event.request_llm(
                        prompt=event.message_str,
                        contexts=active_session.get("context", []),
                        session_id=active_session.get("cid"),
                    )
                    # 成功处理，直接返回
                    return
    
        # 逻辑2: 如果不是沉浸式回复，则为主动插话功能提供支持
        if self.config.get("enable_proactive_reply", True):
            async with self.group_task_lock:
                # 只有在主动插话计时器激活时才缓冲消息
                if group_id in self.active_timers:
                    sender_name = event.get_sender_name() or sender_id
                    message_text = event.message_str.strip()
                    if message_text and len(self.group_chat_buffer[group_id]) < 20:
                        self.group_chat_buffer[group_id].append(
                            f"{sender_name}: {message_text}"
                        )

    # -----------------------------------------------------
    # Feature 2: 主动插话 (Proactive Interjection)
    # -----------------------------------------------------


    async def _immersive_timeout_task(self, group_id: str, user_id: str):
        """沉浸式会话的超时任务。"""
        try:
            timeout = self.config.get("immersive_chat_timeout", 120)
            await asyncio.sleep(timeout)
        
            async with self.immersive_lock:
                # 再次检查，确保会话没有被更新或删除
                if group_id in self.immersive_sessions and self.immersive_sessions[group_id].get("user_id") == user_id:
                    self.immersive_sessions.pop(group_id)
                    logger.info(f"[沉浸式对话] 群 {group_id} 中用户 {user_id} 的沉浸式会话已超时。")
        except asyncio.CancelledError:
            # 任务被取消是正常行为（例如，用户回复了或插件关闭）
            logger.debug(f"[沉浸式对话] 群 {group_id} 中用户 {user_id} 的会话计时器被取消。")
        except Exception as e:
            logger.error(f"[沉浸式对话] 超时任务异常: {e}", exc_info=True)

    # 一个辅助函数，用于封装启动/重置检查任务的逻

    async def _start_proactive_check(self, group_id: str, unified_msg_origin: str):
        """辅助函数，用于启动或重置一个群组的主动插话检查任务。"""
        async with self.group_task_lock:
            # 如果已有计时器，取消它
            if group_id in self.active_timers:
                self.active_timers[group_id].cancel()
                logger.debug(f"[主动插话] 取消了群 {group_id} 的旧计时器。")

            # 清空该群的聊天缓冲区，并启动新的检查任务
            self.group_chat_buffer[group_id].clear()
            task = asyncio.create_task(
                self._proactive_check_task(group_id, unified_msg_origin)
            )
            self.active_timers[group_id] = task
        logger.info(f"[主动插话] 已为群 {group_id} 启动/重置了延时检查任务。")

    @filter.after_message_sent()
    async def after_bot_message_sent(self, event: AstrMessageEvent):
        """机器人发送消息后，启动或重置主动插话的延时检查任务。"""
        if not self.config.get("enable_plugin", True) or not self.config.get(
            "enable_proactive_reply", True
        ):
            return
        if event.is_private_chat():
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # 【修改】调用新的辅助函数来处理任务启动，使代码更简洁
        await self._start_proactive_check(group_id, event.unified_msg_origin)

    async def _proactive_check_task(self, group_id: str, unified_msg_origin: str):
        """延时任务，在指定时间后检查一次是否需要主动插话。"""
        try:
            delay = self.config.get("proactive_reply_delay", 8)
            await asyncio.sleep(delay)

            chat_history = []
            async with self.group_task_lock:
                # 再次确认当前任务是否还是最新的，防止旧任务执行
                if self.active_timers.get(group_id) is not asyncio.current_task():
                    return
                if group_id in self.group_chat_buffer:
                    chat_history = self.group_chat_buffer.pop(group_id, [])

            if not chat_history:
                logger.debug(
                    f"[主动插话] 群 {group_id} 在 {delay}s 内无新消息，任务结束。"
                )
                return

            logger.info(
                f"[主动插话] 群 {group_id} 计时结束，收集到 {len(chat_history)} 条消息，请求LLM判断。"
            )

            # 1. 获取当前会话对应的人格(Persona)的系统提示词
            base_system_prompt = ""
            try:
                uid = unified_msg_origin
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
                conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid) if curr_cid else None

                persona_id = conversation.persona_id if conversation else None
                all_personas = self.context.provider_manager.personas

                # 如果会话有人格设定且不是"无",则查找对应人格
                if persona_id and persona_id != "[%None]":
                    found_persona = next((p for p in all_personas if p.get("name") == persona_id), None)
                    if found_persona:
                        base_system_prompt = found_persona.get("prompt", "")
                # 如果会话未指定人格,则使用默认人格
                elif not persona_id:
                    default_persona_id = self.context.provider_manager.selected_default_persona.get("name")
                    if default_persona_id:
                        found_persona = next((p for p in all_personas if p.get("name") == default_persona_id), None)
                        if found_persona:
                            base_system_prompt = found_persona.get("prompt", "")
            except Exception as e:
                logger.error(f"[主动插话] 获取人格配置时出错: {e}", exc_info=True)


            # 2. 组合最终的系统提示词和用户提示词
            formatted_history = "\n".join(chat_history)
            user_prompt = (
                f"--- 对话记录 ---\n{formatted_history}\n--- 对话记录结束 ---"
            )

            # 您指定的指令
            instruction = (
                "在一个群聊里，在你刚刚说完话之后的一段时间里，群里发生了以下的对话。根据以上对话内容，你是否应该主动插话，"
                "无论无何请严格按照JSON格式在```json ... ```代码块中回答。"
                f'格式示例：\n```json\n{{"should_reply": true, "content": "你的回复内容"}}\n```\n'
                f'或\n```json\n{{"should_reply": false, "content": ""}}\n```'
            )

            final_system_prompt = f"{base_system_prompt}\n\n{instruction}".strip()

            # 3. 调用大语言模型
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[主动插话] 未找到可用的大语言模型提供商。")
                return

            llm_response = await provider.text_chat(prompt=user_prompt, system_prompt=final_system_prompt)
            json_string = self._extract_json_from_text(llm_response.completion_text)
            if not json_string:
                logger.warning(
                    f"[主动插话] 从LLM回复中未能提取出JSON。原始回复: {llm_response.completion_text}"
                )
                return

            try:
                decision_data = json.loads(json_string)
                if decision_data.get("should_reply") and decision_data.get("content"):
                    content = decision_data["content"]
                    logger.info(f"[主动插话] LLM判断需要回复，内容: {content[:50]}...")
                    message_chain = MessageChain().message(content)
                    await self.context.send_message(unified_msg_origin, message_chain)

                    # 成功插话后，立即调用辅助函数，重新启动新一轮的检测
                    logger.info(f"[主动插话] 插话成功，为群 {group_id} 重新启动检测。")
                    await self._start_proactive_check(group_id, unified_msg_origin)

                else:
                    logger.info("[主动插话] LLM判断无需回复。")
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                logger.error(
                    f"[主动插话] 解析LLM的JSON回复失败: {e}\n原始回复: {llm_response.completion_text}\n清理后文本: '{json_string}'"
                )

        except asyncio.CancelledError:
            logger.info(f"[主动插话] 群 {group_id} 的检查任务被取消。")
        except Exception as e:
            logger.error(
                f"[主动插话] 群 {group_id} 的检查任务出现未知异常: {e}", exc_info=True
            )
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
                logger.info(
                    f"[沉浸式对话] 检测到群 {group_id} 的直接回复消息，将携带上下文触发LLM。"
                )
                saved_cid = saved_data.get("cid")
                saved_context = saved_data.get("context", [])
                event.stop_event()
                yield event.request_llm(
                    prompt=event.message_str,
                    contexts=saved_context,
                    session_id=saved_cid,
                )
                return

        # 逻辑2: 为主动插话功能提供支持 (仅在计时器激活时缓冲消息)
        if self.config.get("enable_proactive_reply", True):
            async with self.group_task_lock:
                if group_id in self.active_timers:
                    sender_name = event.get_sender_name() or event.get_sender_id()
                    message_text = event.message_str.strip()
                    if message_text and len(self.group_chat_buffer[group_id]) < 20:
                        self.group_chat_buffer[group_id].append(
                            f"{sender_name}: {message_text}"
                        )

    async def terminate(self):
        """插件被卸载/停用时调用，用于清理"""
        logger.info("正在卸载ReplyDirectly插件，取消所有后台任务...")
        async with self.group_task_lock:
            for task in self.active_timers.values():
                task.cancel()
            self.active_timers.clear()
            self.group_chat_buffer.clear()
        async with self.immersive_lock:
            for session_data in self.immersive_sessions.values():
                if session_data and "task" in session_data:
                    session_data["task"].cancel()
            self.immersive_sessions.clear()
        async with self.immersive_lock:
            self.direct_reply_context.clear()

        logger.info("ReplyDirectly插件所有后台任务已清理。")
