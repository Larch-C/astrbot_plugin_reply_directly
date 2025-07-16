import asyncio
import json
from typing import Dict, List
from collections import defaultdict
import time

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import LLMResponse

# 用于存储需要直接回复的群组，值为True表示下一次消息需要直接回复
# 结构: { "platform:group_id": True }
direct_reply_flags: Dict[str, bool] = {}

# 用于存储群聊消息记录
# 结构: { "platform:group_id": [{"sender": "xxx", "text": "xxx", "time": 12345}] }
group_chat_history: Dict[str, List[Dict]] = defaultdict(list)

# 用于跟踪每个群组的主动回复任务
# 结构: { "platform:group_id": asyncio.Task }
proactive_reply_tasks: Dict[str, asyncio.Task] = {}


@register(
    "reply_directly",
    "YourName",
    "增强机器人对话的沉浸感和主动性，支持免@回复和主动插话。",
    "2.0.0",
    "https://github.com/qa296/astrbot_plugin_reply_directly"
)
class ReplyDirectlyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("ReplyDirectly 插件已加载，人格保持功能已集成。")

    # --- 新增：核心函数，用于获取当前会话的人格 ---
    async def _get_current_system_prompt(self, event: AstrMessageEvent) -> str:
        """获取当前会话的System Prompt，优先使用会话特定人格，否则使用默认人格"""
        try:
            uid = event.unified_msg_origin
            # 尝试获取当前会话对象
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                # 如果没有当前会话，直接获取默认人格
                persona = self.context.provider_manager.get_default_persona()
                logger.debug("当前会话不存在，使用默认人格。")
                return persona.prompt if persona else ""

            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            if not conversation or not conversation.persona_id or conversation.persona_id == "[%None]":
                # 如果会话没有人格或显式取消人格，使用默认人格
                persona = self.context.provider_manager.get_default_persona()
                logger.debug(f"会话 {curr_cid} 未指定人格，使用默认人格。")
                return persona.prompt if persona else ""

            # 使用会话指定的人格
            persona = self.context.provider_manager.get_persona_by_id(conversation.persona_id)
            if persona:
                logger.debug(f"会话 {curr_cid} 正在使用人格: {persona.name}")
                return persona.prompt
            else:
                # 如果指定的人格ID找不到，也回退到默认人格
                logger.warning(f"未找到ID为 {conversation.persona_id} 的人格，回退到默认人格。")
                default_persona = self.context.provider_manager.get_default_persona()
                return default_persona.prompt if default_persona else ""
        except Exception as e:
            logger.error(f"获取System Prompt时发生错误: {e}")
            return "" # 发生错误时返回空字符串，避免影响主流程

    # --- 沉浸式对话功能 ---
    @filter.llm_tool(name="enable_direct_reply_once")
    async def enable_direct_reply_once(self, event: AstrMessageEvent) -> MessageEventResult:
        """
        启用沉浸式对话。调用此函数后，群聊中的下一条消息将无需@机器人，便可直接触发回复。此效果仅生效一次。

        Args:
            - 无
        """
        if not self.config.get("enable_plugin") or not self.config.get("enable_immersive_chat"):
            return

        if event.is_private_chat():
            # 私聊中此功能无意义
            return

        group_id = event.get_group_id()
        platform_name = event.get_platform_name()
        flag_key = f"{platform_name}:{group_id}"
        direct_reply_flags[flag_key] = True
        logger.info(f"已为群组 {flag_key} 设置下一次直接回复标记。")
        # 这个函数工具本身不应该产生任何用户可见的输出
        # 通过返回一个空的result来避免发送任何消息
        return event.empty_result()


    # --- 主动插话功能 ---
    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """当机器人发送消息后，启动一个计时器，用于后续的主动插话判断。"""
        if not self.config.get("enable_plugin") or not self.config.get("enable_proactive_reply"):
            return

        # 只在群聊中生效
        if event.is_private_chat():
            return

        group_id = event.get_group_id()
        platform_name = event.get_platform_name()
        task_key = f"{platform_name}:{group_id}"

        # 如果该群聊已有计时任务，先取消它
        if task_key in proactive_reply_tasks and not proactive_reply_tasks[task_key].done():
            proactive_reply_tasks[task_key].cancel()
            logger.debug(f"已取消群组 {task_key} 的旧主动回复任务。")

        # 清空该群组的短期历史记录，为新的计时做准备
        history_key = f"{platform_name}:{group_id}"
        group_chat_history[history_key].clear()
        
        delay = self.config.get("proactive_reply_delay", 8)
        
        # 修改：在创建任务前，先获取好当前的人格
        system_prompt = await self._get_current_system_prompt(event)

        # 创建新的计时任务
        task = asyncio.create_task(
            self._proactive_reply_task(delay, group_id, platform_name, system_prompt)
        )
        proactive_reply_tasks[task_key] = task
        logger.info(f"已为群组 {task_key} 创建新的主动回复任务，延迟 {delay} 秒。")

    async def _proactive_reply_task(self, delay: int, group_id: str, platform_name: str, system_prompt: str):
        """主动插话的后台任务"""
        try:
            await asyncio.sleep(delay)

            history_key = f"{platform_name}:{group_id}"
            records = group_chat_history.get(history_key, [])

            if not records:
                logger.info(f"群组 {history_key} 在 {delay} 秒内无新消息，不执行主动回复。")
                return

            # 构建上下文
            formatted_history = "\n".join([f"{record['sender']}: {record['text']}" for record in records])
            prompt = (
                f"你正在一个群聊中。以下是最近的几条聊天记录：\n"
                f"---聊天记录开始---\n{formatted_history}\n---聊天记录结束---\n\n"
                f"请根据以上内容，判断你是否需要插话参与讨论。你的判断应基于以下几点：\n"
                f"1. 话题是否与你的知识领域或人设相关？\n"
                f"2. 你的回复是否能提供价值（如信息、帮助、趣味）？\n"
                f"3. 对话是否处于一个适合你加入的节点？\n\n"
                f"请严格按照以下JSON格式返回你的决定，不要添加任何额外的解释：\n"
                f'{{"should_reply": boolean, "reply_content": "string"}}'
            )

            logger.debug(f"为群组 {history_key} 构建的主动回复判断Prompt: {prompt}")

            # 调用LLM进行判断
            llm_response = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                # 修改：直接使用传入的人格
                system_prompt=system_prompt  
            )

            if llm_response.role == "assistant":
                decision_text = llm_response.completion_text
                try:
                    decision = json.loads(decision_text)
                    if decision.get("should_reply"):
                        reply_content = decision.get("reply_content", "")
                        if reply_content:
                            logger.info(f"LLM决定在群组 {history_key} 中主动插话，内容: {reply_content}")
                            # 使用 context.send_message 来主动发送消息
                            umo = f"{platform_name}:group:{group_id}"
                            await self.context.send_message_by_text(umo, reply_content)
                        else:
                            logger.info(f"LLM决定回复但内容为空，不发送。")
                    else:
                        logger.info(f"LLM决定不在群组 {history_key} 中主动插话。")
                except json.JSONDecodeError:
                    logger.error(f"解析LLM主动回复决策失败，原始文本: {decision_text}")
            
            # 任务完成，清空历史记录
            group_chat_history[history_key].clear()

        except asyncio.CancelledError:
            logger.info(f"群组 {platform_name}:{group_id} 的主动回复任务被取消。")
        except Exception as e:
            logger.error(f"主动回复任务执行失败: {e}")


    # --- 消息监听与处理 ---
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理所有群消息，用于沉浸式对话和主动插话的数据收集。"""
        if not self.config.get("enable_plugin"):
            return

        group_id = event.get_group_id()
        platform_name = event.get_platform_name()
        
        # --- 沉浸式对话处理逻辑 ---
        if self.config.get("enable_immersive_chat"):
            flag_key = f"{platform_name}:{group_id}"
            if direct_reply_flags.get(flag_key):
                # 标记存在，消耗它
                direct_reply_flags[flag_key] = False
                logger.info(f"检测到群组 {flag_key} 的直接回复标记，消耗标记并触发LLM。")
                
                # 修改：在请求LLM前，获取当前人格
                system_prompt = await self._get_current_system_prompt(event)
                
                # 直接将消息请求LLM
                yield event.request_llm(
                    prompt=event.message_str,
                    system_prompt=system_prompt # 应用人格
                )
                # 停止事件传播，避免其他插件或默认回复处理
                event.stop_event()
                return

        # --- 主动插话历史记录收集 ---
        if self.config.get("enable_proactive_reply"):
            history_key = f"{platform_name}:{group_id}"
            # 只有在有主动回复任务时才记录
            if history_key in proactive_reply_tasks and not proactive_reply_tasks[history_key].done():
                group_chat_history[history_key].append({
                    "sender": event.get_sender_name(),
                    "text": event.message_str,
                    "time": time.time()
                })
                logger.debug(f"为群组 {history_key} 添加聊天记录: {event.message_str}")
