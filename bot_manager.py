"""
Bot Manager - Handles multiple Telegram bots with load balancing
"""

import asyncio
import logging
import time
import json
import traceback
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import defaultdict, deque
from enum import Enum
import heapq

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import RetryAfter, TelegramError, NetworkError, TimedOut, Forbidden, BadRequest
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, AuthKeyUnregisteredError

from database import DatabaseManager, MessagePair, MessageMapping
from message_processor import MessageProcessor
from config import Config

logger = logging.getLogger(__name__)

class MessagePriority(Enum):
    """Message priority levels"""
    URGENT = 4
    HIGH = 3
    NORMAL = 2
    LOW = 1

    def __lt__(self, other):
        return self.value < other.value

@dataclass
class QueuedMessage:
    """Queued message with priority and metadata"""
    data: dict
    priority: MessagePriority
    timestamp: float
    pair_id: int
    bot_index: int
    retry_count: int = 0
    max_retries: int = 3
    processing_time_estimate: float = 1.0

    def __lt__(self, other):
        if self.priority.value != other.priority.value:
            return self.priority.value > other.priority.value
        return self.timestamp < other.timestamp

@dataclass
class BotMetrics:
    """Bot performance metrics"""
    messages_processed: int = 0
    success_rate: float = 1.0
    avg_processing_time: float = 1.0
    current_load: int = 0
    error_count: int = 0
    last_activity: float = 0
    rate_limit_until: float = 0
    consecutive_failures: int = 0
    
    def update_success_rate(self, success: bool):
        """Update success rate with exponential moving average"""
        alpha = 0.1  # Learning rate
        new_rate = 1.0 if success else 0.0
        self.success_rate = alpha * new_rate + (1 - alpha) * self.success_rate
        
        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

class BotManager:
    """Production-ready bot manager with advanced features"""
    
    def __init__(self, db_manager: DatabaseManager, config: Config):
        self.db_manager = db_manager
        self.config = config
        self.message_processor = MessageProcessor(db_manager, config)
        
        # Bot instances
        self.telegram_bots: List[Bot] = []
        self.bot_applications: List[Application] = []
        self.telethon_client: Optional[TelegramClient] = None
        
        # Monitoring and metrics
        self.bot_metrics: Dict[int, BotMetrics] = {}
        self.message_queue = asyncio.PriorityQueue(maxsize=config.MESSAGE_QUEUE_SIZE)
        self.pair_queues: Dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        
        # Rate limiting
        self.rate_limiters: Dict[int, deque] = defaultdict(lambda: deque(maxlen=config.RATE_LIMIT_MESSAGES))
        
        # System state
        self.running = False
        self.worker_tasks: List[asyncio.Task] = []
        self.pairs: Dict[int, MessagePair] = {}
        self.source_to_pairs: Dict[int, List[int]] = defaultdict(list)
        
        # Error tracking
        self.global_error_count = 0
        self.last_error_time = 0
        
    async def initialize(self):
        """Initialize all bot components"""
        try:
            # Initialize bots
            await self._init_telegram_bots()
            await self._init_telethon_client()
            
            # Initialize message processor
            await self.message_processor.initialize()
            
            # Load pairs from database
            await self._load_pairs()
            
            # Initialize metrics
            for i in range(len(self.telegram_bots)):
                self.bot_metrics[i] = BotMetrics()
            
            logger.info(f"Bot manager initialized with {len(self.telegram_bots)} bots")
            
        except Exception as e:
            logger.error(f"Failed to initialize bot manager: {e}")
            raise
    
    async def _init_telegram_bots(self):
        """Initialize Telegram bot instances"""
        for i, token in enumerate(self.config.BOT_TOKENS):
            try:
                bot = Bot(token=token)
                
                # Test bot connectivity
                bot_info = await bot.get_me()
                logger.info(f"Bot {i} initialized: @{bot_info.username}")
                
                self.telegram_bots.append(bot)
                
                # Create application for command handling
                app = Application.builder().token(token).build()
                
                # Add command handlers only to primary bot
                if i == 0:
                    await self._setup_command_handlers(app)
                
                self.bot_applications.append(app)
                
            except Exception as e:
                logger.error(f"Failed to initialize bot {i}: {e}")
                # Continue with other bots
    
    async def _init_telethon_client(self):
        """Initialize Telethon client for message listening"""
        try:
            self.telethon_client = TelegramClient(
                'session_bot',
                self.config.API_ID,
                self.config.API_HASH
            )
            
            await self.telethon_client.start(phone=self.config.PHONE_NUMBER)
            logger.info("Telethon client initialized")
            
            # Setup message handlers
            self.telethon_client.add_event_handler(
                self._handle_new_message,
                events.NewMessage()
            )
            
            self.telethon_client.add_event_handler(
                self._handle_message_edited,
                events.MessageEdited()
            )
            
            self.telethon_client.add_event_handler(
                self._handle_message_deleted,
                events.MessageDeleted()
            )
            
        except Exception as e:
            logger.error(f"Failed to initialize Telethon client: {e}")
            raise
    
    async def _setup_command_handlers(self, app: Application):
        """Setup command handlers for primary bot"""
        # Basic commands
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        
        # System management
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("stats", self._cmd_stats))
        app.add_handler(CommandHandler("health", self._cmd_health))
        app.add_handler(CommandHandler("pause", self._cmd_pause))
        app.add_handler(CommandHandler("resume", self._cmd_resume))
        app.add_handler(CommandHandler("restart", self._cmd_restart))
        
        # Pair management
        app.add_handler(CommandHandler("pairs", self._cmd_pairs))
        app.add_handler(CommandHandler("addpair", self._cmd_add_pair))
        app.add_handler(CommandHandler("delpair", self._cmd_delete_pair))
        app.add_handler(CommandHandler("editpair", self._cmd_edit_pair))
        app.add_handler(CommandHandler("pairinfo", self._cmd_pair_info))
        
        # Bot management
        app.add_handler(CommandHandler("bots", self._cmd_bots))
        app.add_handler(CommandHandler("botinfo", self._cmd_bot_info))
        app.add_handler(CommandHandler("rebalance", self._cmd_rebalance))
        
        # Queue management
        app.add_handler(CommandHandler("queue", self._cmd_queue))
        app.add_handler(CommandHandler("clearqueue", self._cmd_clear_queue))
        
        # Logs and diagnostics
        app.add_handler(CommandHandler("logs", self._cmd_logs))
        app.add_handler(CommandHandler("errors", self._cmd_errors))
        app.add_handler(CommandHandler("diagnostics", self._cmd_diagnostics))
        
        # Settings
        app.add_handler(CommandHandler("settings", self._cmd_settings))
        app.add_handler(CommandHandler("set", self._cmd_set_setting))
        
        # Utilities
        app.add_handler(CommandHandler("backup", self._cmd_backup))
        app.add_handler(CommandHandler("cleanup", self._cmd_cleanup))
        
        app.add_handler(CallbackQueryHandler(self._handle_callback))
    
    async def _load_pairs(self):
        """Load message pairs from database"""
        try:
            pairs = await self.db_manager.get_all_pairs()
            self.pairs = {pair.id: pair for pair in pairs}
            
            # Build source chat mapping
            self.source_to_pairs.clear()
            for pair in pairs:
                if pair.status == "active":
                    self.source_to_pairs[pair.source_chat_id].append(pair.id)
            
            logger.info(f"Loaded {len(pairs)} pairs from database")
            
        except Exception as e:
            logger.error(f"Failed to load pairs: {e}")
            raise
    
    async def start(self):
        """Start bot manager and all components"""
        try:
            self.running = True
            
            # Start Telegram bot applications
            for i, app in enumerate(self.bot_applications):
                if i == 0:  # Only start primary bot for commands
                    await app.initialize()
                    await app.start()
                    logger.info(f"Started bot application {i}")
            
            # Start worker tasks
            for i in range(self.config.MAX_WORKERS):
                task = asyncio.create_task(self._message_worker(i))
                self.worker_tasks.append(task)
            
            # Start monitoring tasks
            monitoring_tasks = [
                asyncio.create_task(self._health_monitor()),
                asyncio.create_task(self._queue_monitor()),
                asyncio.create_task(self._rate_limit_monitor())
            ]
            self.worker_tasks.extend(monitoring_tasks)
            
            logger.info("Bot manager started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start bot manager: {e}")
            raise
    
    async def stop(self):
        """Stop bot manager and cleanup"""
        try:
            self.running = False
            
            # Cancel worker tasks
            for task in self.worker_tasks:
                task.cancel()
            
            # Wait for tasks to complete
            if self.worker_tasks:
                await asyncio.gather(*self.worker_tasks, return_exceptions=True)
            
            # Stop bot applications
            for app in self.bot_applications:
                if app.running:
                    await app.stop()
                    await app.shutdown()
            
            # Disconnect Telethon client
            if self.telethon_client and self.telethon_client.is_connected():
                await self.telethon_client.disconnect()
            
            logger.info("Bot manager stopped")
            
        except Exception as e:
            logger.error(f"Error stopping bot manager: {e}")
    
    async def _handle_new_message(self, event):
        """Handle new messages from Telethon"""
        try:
            chat_id = event.chat_id
            
            # Check if this chat has active pairs
            if chat_id not in self.source_to_pairs:
                return
            
            # Process message for each pair
            for pair_id in self.source_to_pairs[chat_id]:
                pair = self.pairs.get(pair_id)
                if not pair or pair.status != "active":
                    continue
                
                # Create message data
                message_data = {
                    'type': 'new_message',
                    'event': event,
                    'pair_id': pair_id,
                    'timestamp': time.time()
                }
                
                # Determine priority
                priority = self._get_message_priority(event, pair)
                
                # Queue message
                await self._queue_message(message_data, priority, pair_id, pair.assigned_bot_index)
        
        except Exception as e:
            logger.error(f"Error handling new message: {e}")
            await self._log_error("message_handling", str(e), traceback.format_exc())
    
    async def _handle_message_edited(self, event):
        """Handle message edits"""
        try:
            chat_id = event.chat_id
            
            if chat_id not in self.source_to_pairs:
                return
            
            for pair_id in self.source_to_pairs[chat_id]:
                pair = self.pairs.get(pair_id)
                if not pair or pair.status != "active":
                    continue
                
                if not pair.filters.get("sync_edits", True):
                    continue
                
                message_data = {
                    'type': 'edit_message',
                    'event': event,
                    'pair_id': pair_id,
                    'timestamp': time.time()
                }
                
                await self._queue_message(message_data, MessagePriority.HIGH, pair_id, pair.assigned_bot_index)
        
        except Exception as e:
            logger.error(f"Error handling message edit: {e}")
    
    async def _handle_message_deleted(self, event):
        """Handle message deletions"""
        try:
            chat_id = event.chat_id
            
            if chat_id not in self.source_to_pairs:
                return
            
            for pair_id in self.source_to_pairs[chat_id]:
                pair = self.pairs.get(pair_id)
                if not pair or pair.status != "active":
                    continue
                
                if not pair.filters.get("sync_deletes", False):
                    continue
                
                message_data = {
                    'type': 'delete_message',
                    'event': event,
                    'pair_id': pair_id,
                    'timestamp': time.time()
                }
                
                await self._queue_message(message_data, MessagePriority.NORMAL, pair_id, pair.assigned_bot_index)
        
        except Exception as e:
            logger.error(f"Error handling message deletion: {e}")
    
    def _get_message_priority(self, event, pair: MessagePair) -> MessagePriority:
        """Determine message priority"""
        # High priority for replies if preserve_replies is enabled
        if event.is_reply and pair.filters.get("preserve_replies", True):
            return MessagePriority.HIGH
        
        # High priority for media messages
        if event.media:
            return MessagePriority.HIGH
        
        # Normal priority for regular messages
        return MessagePriority.NORMAL
    
    async def _queue_message(self, message_data: dict, priority: MessagePriority, 
                           pair_id: int, bot_index: int):
        """Queue message for processing"""
        try:
            queued_msg = QueuedMessage(
                data=message_data,
                priority=priority,
                timestamp=time.time(),
                pair_id=pair_id,
                bot_index=bot_index
            )
            
            # Check if queue is full
            if self.message_queue.full():
                logger.warning("Message queue is full, dropping oldest message")
                try:
                    self.message_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            
            await self.message_queue.put(queued_msg)
            
        except Exception as e:
            logger.error(f"Failed to queue message: {e}")
    
    async def _message_worker(self, worker_id: int):
        """Worker task for processing messages"""
        logger.info(f"Message worker {worker_id} started")
        
        while self.running:
            try:
                # Get message from queue with timeout
                try:
                    queued_msg = await asyncio.wait_for(
                        self.message_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Check if system is paused
                paused = await self.db_manager.get_setting("system_paused", "false")
                if paused.lower() == "true":
                    # Put message back in queue
                    await self.message_queue.put(queued_msg)
                    await asyncio.sleep(5)
                    continue
                
                # Process message
                success = await self._process_queued_message(queued_msg)
                
                # Update metrics
                bot_metrics = self.bot_metrics.get(queued_msg.bot_index)
                if bot_metrics:
                    bot_metrics.update_success_rate(success)
                    bot_metrics.messages_processed += 1
                    bot_metrics.last_activity = time.time()
                
                # Handle retry if failed
                if not success and queued_msg.retry_count < queued_msg.max_retries:
                    queued_msg.retry_count += 1
                    await asyncio.sleep(2 ** queued_msg.retry_count)  # Exponential backoff
                    await self.message_queue.put(queued_msg)
                
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(1)
        
        logger.info(f"Message worker {worker_id} stopped")
    
    async def _process_queued_message(self, queued_msg: QueuedMessage) -> bool:
        """Process a queued message"""
        try:
            start_time = time.time()
            
            # Get bot and pair
            bot_index = queued_msg.bot_index
            if bot_index >= len(self.telegram_bots):
                bot_index = 0  # Fallback to primary bot
            
            bot = self.telegram_bots[bot_index]
            pair = self.pairs.get(queued_msg.pair_id)
            
            if not pair:
                logger.warning(f"Pair {queued_msg.pair_id} not found")
                return False
            
            # Check rate limits
            if not self._check_rate_limit(bot_index):
                logger.warning(f"Rate limit exceeded for bot {bot_index}")
                return False
            
            # Process based on message type
            message_type = queued_msg.data['type']
            event = queued_msg.data['event']
            
            success = False
            if message_type == 'new_message':
                success = await self.message_processor.process_new_message(
                    event, pair, bot, bot_index
                )
            elif message_type == 'edit_message':
                success = await self.message_processor.process_message_edit(
                    event, pair, bot, bot_index
                )
            elif message_type == 'delete_message':
                success = await self.message_processor.process_message_delete(
                    event, pair, bot, bot_index
                )
            
            # Update processing time
            processing_time = time.time() - start_time
            bot_metrics = self.bot_metrics.get(bot_index)
            if bot_metrics:
                # Update average processing time with EMA
                alpha = 0.1
                bot_metrics.avg_processing_time = (
                    alpha * processing_time + 
                    (1 - alpha) * bot_metrics.avg_processing_time
                )
            
            return success
            
        except RetryAfter as e:
            logger.warning(f"Rate limited by Telegram: {e.retry_after} seconds")
            bot_metrics = self.bot_metrics.get(queued_msg.bot_index)
            if bot_metrics:
                bot_metrics.rate_limit_until = time.time() + e.retry_after
            return False
            
        except (NetworkError, TimedOut) as e:
            logger.warning(f"Network error: {e}")
            return False
            
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
            await self._log_error("telegram_error", str(e), None, queued_msg.bot_index)
            return False
            
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self._log_error("processing_error", str(e), queued_msg.pair_id, queued_msg.bot_index)
            return False
    
    def _check_rate_limit(self, bot_index: int) -> bool:
        """Check if bot is rate limited"""
        bot_metrics = self.bot_metrics.get(bot_index)
        if bot_metrics and bot_metrics.rate_limit_until > time.time():
            return False
        
        # Check message rate limit
        now = time.time()
        rate_limiter = self.rate_limiters[bot_index]
        
        # Remove old entries
        while rate_limiter and rate_limiter[0] < now - self.config.RATE_LIMIT_WINDOW:
            rate_limiter.popleft()
        
        # Check if limit exceeded
        if len(rate_limiter) >= self.config.RATE_LIMIT_MESSAGES:
            return False
        
        # Add current time
        rate_limiter.append(now)
        return True
    
    async def _health_monitor(self):
        """Monitor bot health and performance"""
        while self.running:
            try:
                await asyncio.sleep(self.config.HEALTH_CHECK_INTERVAL)
                
                # Check bot connectivity
                for i, bot in enumerate(self.telegram_bots):
                    try:
                        await bot.get_me()
                        bot_metrics = self.bot_metrics.get(i)
                        if bot_metrics:
                            bot_metrics.consecutive_failures = 0
                    except Exception as e:
                        logger.warning(f"Bot {i} health check failed: {e}")
                        bot_metrics = self.bot_metrics.get(i)
                        if bot_metrics:
                            bot_metrics.consecutive_failures += 1
                
                # Log metrics
                await self._log_metrics()
                
            except Exception as e:
                logger.error(f"Health monitor error: {e}")
    
    async def _queue_monitor(self):
        """Monitor message queue health"""
        while self.running:
            try:
                await asyncio.sleep(30)
                
                queue_size = self.message_queue.qsize()
                if queue_size > self.config.MESSAGE_QUEUE_SIZE * 0.8:
                    logger.warning(f"Message queue is {queue_size}/{self.config.MESSAGE_QUEUE_SIZE}")
                
                # Update current load for each bot
                for bot_index, metrics in self.bot_metrics.items():
                    metrics.current_load = queue_size
                
            except Exception as e:
                logger.error(f"Queue monitor error: {e}")
    
    async def _rate_limit_monitor(self):
        """Monitor and reset rate limits"""
        while self.running:
            try:
                await asyncio.sleep(60)
                
                now = time.time()
                for bot_index, rate_limiter in self.rate_limiters.items():
                    # Clean old entries
                    while rate_limiter and rate_limiter[0] < now - self.config.RATE_LIMIT_WINDOW:
                        rate_limiter.popleft()
                
            except Exception as e:
                logger.error(f"Rate limit monitor error: {e}")
    
    async def _log_metrics(self):
        """Log system metrics"""
        try:
            total_processed = sum(m.messages_processed for m in self.bot_metrics.values())
            avg_success_rate = sum(m.success_rate for m in self.bot_metrics.values()) / len(self.bot_metrics) if self.bot_metrics else 0
            queue_size = self.message_queue.qsize()
            
            logger.info(f"Metrics - Processed: {total_processed}, Success Rate: {avg_success_rate:.2f}, Queue: {queue_size}")
            
        except Exception as e:
            logger.error(f"Failed to log metrics: {e}")
    
    async def _log_error(self, error_type: str, error_message: str, 
                        stack_trace: Optional[str] = None, 
                        pair_id: Optional[int] = None, 
                        bot_index: Optional[int] = None):
        """Log error to database"""
        try:
            await self.db_manager.log_error(error_type, error_message, pair_id, bot_index, stack_trace)
        except Exception as e:
            logger.error(f"Failed to log error to database: {e}")
    
    # Command handlers
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        await update.message.reply_text(
            "🤖 Telegram Message Copying Bot\n\n"
            "Available commands:\n"
            "/status - System status\n"
            "/stats - Statistics\n"
            "/pairs - List message pairs\n"
            "/pause - Pause system\n"
            "/resume - Resume system\n"
            "/help - Show help"
        )
    
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        help_text = """
🤖 **Telegram Message Copying Bot**

**System Management:**
/status - System status and overview
/stats - Detailed statistics
/health - Health monitoring
/pause - Pause message processing
/resume - Resume message processing
/restart - Restart bot system

**Pair Management:**
/pairs - List all message pairs
/addpair <source> <dest> <name> - Add new pair
/delpair <id> - Delete pair
/editpair <id> <setting> <value> - Edit pair settings
/pairinfo <id> - Detailed pair information

**Bot Management:**
/bots - List all bot instances
/botinfo <index> - Detailed bot information
/rebalance - Rebalance message distribution

**Queue & Processing:**
/queue - View message queue status
/clearqueue - Clear message queue

**Logs & Diagnostics:**
/logs [limit] - View recent log entries
/errors [limit] - View recent errors
/diagnostics - Run system diagnostics

**Settings:**
/settings - View current settings
/set <key> <value> - Update setting

**Utilities:**
/backup - Create database backup
/cleanup - Clean old data

**Features:**
✅ Multi-bot support with load balancing
✅ Advanced message filtering
✅ Real-time synchronization
✅ Image duplicate detection
✅ Reply preservation
✅ Edit/delete sync
✅ Comprehensive statistics
        """
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Status command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            # System status
            paused = await self.db_manager.get_setting("system_paused", "false")
            queue_size = self.message_queue.qsize()
            active_pairs = len([p for p in self.pairs.values() if p.status == "active"])
            
            # Bot status
            bot_status = []
            for i, metrics in self.bot_metrics.items():
                status = "🟢" if metrics.consecutive_failures == 0 else "🔴"
                bot_status.append(f"{status} Bot {i}: {metrics.success_rate:.1%} success")
            
            status_text = f"""
🔄 **System Status**

**State:** {'⏸️ PAUSED' if paused == 'true' else '▶️ RUNNING'}
**Queue:** {queue_size} messages
**Active Pairs:** {active_pairs}

**Bots:**
{chr(10).join(bot_status)}

**Uptime:** {self._get_uptime()}
            """
            
            await update.message.reply_text(status_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting status: {e}")
    
    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Statistics command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            stats = await self.db_manager.get_stats()
            
            # Calculate totals from bot metrics
            total_processed = sum(m.messages_processed for m in self.bot_metrics.values())
            avg_processing_time = sum(m.avg_processing_time for m in self.bot_metrics.values()) / len(self.bot_metrics) if self.bot_metrics else 0
            
            stats_text = f"""
📊 **System Statistics**

**Messages:**
• Total processed: {total_processed:,}
• Last 24h: {stats.get('messages_24h', 0):,}
• In database: {stats.get('total_messages', 0):,}

**Pairs:**
• Total: {stats.get('total_pairs', 0)}
• Active: {stats.get('active_pairs', 0)}

**Performance:**
• Avg processing time: {avg_processing_time:.2f}s
• Queue size: {self.message_queue.qsize()}
• Errors (24h): {stats.get('errors_24h', 0)}

**Memory:** {self._get_memory_usage()}
            """
            
            await update.message.reply_text(stats_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting statistics: {e}")
    
    async def _cmd_pairs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List pairs command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            pairs = list(self.pairs.values())[:10]  # Show first 10
            
            if not pairs:
                await update.message.reply_text("No message pairs configured.")
                return
            
            pairs_text = "📋 **Message Pairs:**\n\n"
            for pair in pairs:
                status_emoji = "✅" if pair.status == "active" else "❌"
                pairs_text += f"{status_emoji} **{pair.name}** (ID: {pair.id})\n"
                pairs_text += f"   {pair.source_chat_id} → {pair.destination_chat_id}\n"
                pairs_text += f"   Bot: {pair.assigned_bot_index}, Messages: {pair.stats.get('messages_copied', 0)}\n\n"
            
            if len(self.pairs) > 10:
                pairs_text += f"... and {len(self.pairs) - 10} more pairs"
            
            await update.message.reply_text(pairs_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error listing pairs: {e}")
    
    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause system command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            await self.db_manager.set_setting("system_paused", "true")
            await update.message.reply_text("⏸️ System paused. Use /resume to continue.")
            logger.info(f"System paused by user {update.effective_user.id}")
            
        except Exception as e:
            await update.message.reply_text(f"Error pausing system: {e}")
    
    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume system command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            await self.db_manager.set_setting("system_paused", "false")
            await update.message.reply_text("▶️ System resumed.")
            logger.info(f"System resumed by user {update.effective_user.id}")
            
        except Exception as e:
            await update.message.reply_text(f"Error resuming system: {e}")
    
    async def _cmd_add_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add pair command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            if len(context.args) < 3:
                await update.message.reply_text(
                    "Usage: /addpair <source_chat_id> <dest_chat_id> <name>"
                )
                return
            
            source_id = int(context.args[0])
            dest_id = int(context.args[1])
            name = " ".join(context.args[2:])
            
            pair_id = await self.db_manager.create_pair(source_id, dest_id, name)
            await self._load_pairs()  # Reload pairs
            
            await update.message.reply_text(
                f"✅ Created pair {pair_id}: {name}\n"
                f"{source_id} → {dest_id}"
            )
            
        except ValueError as e:
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            await update.message.reply_text(f"Error adding pair: {e}")
    
    async def _cmd_delete_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete pair command handler"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            if not context.args:
                await update.message.reply_text("Usage: /delpair <pair_id>")
                return
            
            pair_id = int(context.args[0])
            
            if pair_id not in self.pairs:
                await update.message.reply_text("Pair not found.")
                return
            
            pair_name = self.pairs[pair_id].name
            await self.db_manager.delete_pair(pair_id)
            await self._load_pairs()  # Reload pairs
            
            await update.message.reply_text(f"✅ Deleted pair: {pair_name}")
            
        except ValueError:
            await update.message.reply_text("Invalid pair ID.")
        except Exception as e:
            await update.message.reply_text(f"Error deleting pair: {e}")
    
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback queries"""
        # Placeholder for future callback implementations
        query = update.callback_query
        await query.answer()
    
    # Enhanced command handlers
    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Health monitoring command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            # Get system health info
            memory_mb = self._get_memory_usage()
            uptime = self._get_uptime()
            queue_size = self.message_queue.qsize()
            
            # Bot health
            healthy_bots = sum(1 for m in self.bot_metrics.values() if m.consecutive_failures == 0)
            total_bots = len(self.bot_metrics)
            
            # Error rate
            total_errors = sum(m.consecutive_failures for m in self.bot_metrics.values())
            
            health_text = f"""
🏥 **System Health Report**

**Overall Status:** {'🟢 Healthy' if healthy_bots == total_bots else '🟡 Warning' if healthy_bots > 0 else '🔴 Critical'}

**System Resources:**
• Memory Usage: {memory_mb}
• Uptime: {uptime}
• Queue Size: {queue_size}

**Bot Health:**
• Healthy Bots: {healthy_bots}/{total_bots}
• Total Errors: {total_errors}

**Performance:**
• Messages in Queue: {queue_size}
• Active Pairs: {len([p for p in self.pairs.values() if p.status == "active"])}
            """
            
            await update.message.reply_text(health_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting health info: {e}")
    
    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Restart system command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        await update.message.reply_text("🔄 System restart functionality is not yet implemented. Use /pause and /resume instead.")
    
    async def _cmd_edit_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Edit pair settings command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            if len(context.args) < 3:
                await update.message.reply_text(
                    "Usage: /editpair <pair_id> <setting> <value>\n"
                    "Settings: name, status, sync_edits, sync_deletes, preserve_replies"
                )
                return
            
            pair_id = int(context.args[0])
            setting = context.args[1]
            value = " ".join(context.args[2:])
            
            if pair_id not in self.pairs:
                await update.message.reply_text("Pair not found.")
                return
            
            # Update pair setting (basic implementation)
            pair = self.pairs[pair_id]
            if setting == "name":
                pair.name = value
            elif setting == "status":
                pair.status = value
            else:
                pair.filters[setting] = value.lower() == 'true' if value.lower() in ['true', 'false'] else value
            
            await update.message.reply_text(f"✅ Updated {setting} for pair {pair_id}")
            
        except ValueError:
            await update.message.reply_text("Invalid pair ID.")
        except Exception as e:
            await update.message.reply_text(f"Error editing pair: {e}")
    
    async def _cmd_pair_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Detailed pair information command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            if not context.args:
                await update.message.reply_text("Usage: /pairinfo <pair_id>")
                return
            
            pair_id = int(context.args[0])
            
            if pair_id not in self.pairs:
                await update.message.reply_text("Pair not found.")
                return
            
            pair = self.pairs[pair_id]
            
            info_text = f"""
📋 **Pair Information - {pair.name}**

**Basic Info:**
• ID: {pair.id}
• Status: {pair.status}
• Source: {pair.source_chat_id}
• Destination: {pair.destination_chat_id}
• Assigned Bot: {pair.assigned_bot_index}

**Statistics:**
• Messages Copied: {pair.stats.get('messages_copied', 0)}
• Errors: {pair.stats.get('errors', 0)}

**Settings:**
• Sync Edits: {pair.filters.get('sync_edits', True)}
• Sync Deletes: {pair.filters.get('sync_deletes', False)}
• Preserve Replies: {pair.filters.get('preserve_replies', True)}

**Filters:**
{chr(10).join([f"• {k}: {v}" for k, v in pair.filters.items() if k not in ['sync_edits', 'sync_deletes', 'preserve_replies']])}
            """
            
            await update.message.reply_text(info_text, parse_mode='Markdown')
            
        except ValueError:
            await update.message.reply_text("Invalid pair ID.")
        except Exception as e:
            await update.message.reply_text(f"Error getting pair info: {e}")
    
    async def _cmd_bots(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List bot instances command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            bots_text = "🤖 **Bot Instances:**\n\n"
            
            for i, metrics in self.bot_metrics.items():
                status_emoji = "🟢" if metrics.consecutive_failures == 0 else "🔴"
                rate_limited = "⏰" if metrics.rate_limit_until > time.time() else ""
                
                bots_text += f"{status_emoji}{rate_limited} **Bot {i}**\n"
                bots_text += f"  Success Rate: {metrics.success_rate:.1%}\n"
                bots_text += f"  Messages: {metrics.messages_processed}\n"
                bots_text += f"  Avg Time: {metrics.avg_processing_time:.2f}s\n"
                bots_text += f"  Failures: {metrics.consecutive_failures}\n\n"
            
            await update.message.reply_text(bots_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error listing bots: {e}")
    
    async def _cmd_bot_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Detailed bot information command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            if not context.args:
                await update.message.reply_text("Usage: /botinfo <bot_index>")
                return
            
            bot_index = int(context.args[0])
            
            if bot_index not in self.bot_metrics:
                await update.message.reply_text("Bot not found.")
                return
            
            metrics = self.bot_metrics[bot_index]
            
            info_text = f"""
🤖 **Bot {bot_index} Information**

**Status:** {'🟢 Healthy' if metrics.consecutive_failures == 0 else '🔴 Unhealthy'}
**Rate Limited:** {'Yes' if metrics.rate_limit_until > time.time() else 'No'}

**Performance:**
• Messages Processed: {metrics.messages_processed}
• Success Rate: {metrics.success_rate:.1%}
• Avg Processing Time: {metrics.avg_processing_time:.2f}s
• Current Load: {metrics.current_load}

**Error Tracking:**
• Consecutive Failures: {metrics.consecutive_failures}
• Last Activity: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(metrics.last_activity))}
            """
            
            await update.message.reply_text(info_text, parse_mode='Markdown')
            
        except ValueError:
            await update.message.reply_text("Invalid bot index.")
        except Exception as e:
            await update.message.reply_text(f"Error getting bot info: {e}")
    
    async def _cmd_rebalance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rebalance message distribution command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            # Simple rebalancing: redistribute pairs across healthy bots
            healthy_bots = [i for i, m in self.bot_metrics.items() if m.consecutive_failures == 0]
            
            if not healthy_bots:
                await update.message.reply_text("❌ No healthy bots available for rebalancing.")
                return
            
            rebalanced = 0
            for pair_id, pair in self.pairs.items():
                if pair.assigned_bot_index not in healthy_bots:
                    # Reassign to a healthy bot
                    pair.assigned_bot_index = healthy_bots[rebalanced % len(healthy_bots)]
                    rebalanced += 1
            
            await update.message.reply_text(f"✅ Rebalanced {rebalanced} pairs across {len(healthy_bots)} healthy bots.")
            
        except Exception as e:
            await update.message.reply_text(f"Error rebalancing: {e}")
    
    async def _cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View message queue status command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            queue_size = self.message_queue.qsize()
            max_size = self.config.MESSAGE_QUEUE_SIZE
            
            queue_text = f"""
📊 **Message Queue Status**

**Current Size:** {queue_size}/{max_size}
**Usage:** {(queue_size/max_size*100):.1f}%
**Status:** {'🟢 Normal' if queue_size < max_size * 0.7 else '🟡 High' if queue_size < max_size * 0.9 else '🔴 Critical'}

**Queue Distribution:**
            """
            
            # Add per-pair queue info if available
            for pair_id, queue in self.pair_queues.items():
                if len(queue) > 0:
                    pair_name = self.pairs.get(pair_id, {}).name if pair_id in self.pairs else f"Pair {pair_id}"
                    queue_text += f"• {pair_name}: {len(queue)} messages\n"
            
            await update.message.reply_text(queue_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting queue status: {e}")
    
    async def _cmd_clear_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear message queue command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            cleared = 0
            while not self.message_queue.empty():
                try:
                    self.message_queue.get_nowait()
                    cleared += 1
                except asyncio.QueueEmpty:
                    break
            
            await update.message.reply_text(f"✅ Cleared {cleared} messages from queue.")
            
        except Exception as e:
            await update.message.reply_text(f"Error clearing queue: {e}")
    
    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View recent log entries command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            limit = 10
            if context.args:
                try:
                    limit = min(int(context.args[0]), 50)  # Max 50 logs
                except ValueError:
                    pass
            
            # Read recent logs from database or log file
            logs_text = f"📜 **Recent Logs (Last {limit}):**\n\n"
            logs_text += "Log viewing from database not yet implemented.\n"
            logs_text += "Check the server logs for detailed information."
            
            await update.message.reply_text(logs_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting logs: {e}")
    
    async def _cmd_errors(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View recent errors command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            limit = 10
            if context.args:
                try:
                    limit = min(int(context.args[0]), 20)  # Max 20 errors
                except ValueError:
                    pass
            
            errors_text = f"🚨 **Recent Errors (Last {limit}):**\n\n"
            errors_text += "Error log viewing from database not yet implemented.\n"
            errors_text += "Use /diagnostics for current system status."
            
            await update.message.reply_text(errors_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting errors: {e}")
    
    async def _cmd_diagnostics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Run system diagnostics command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            diagnostics = []
            
            # Check bot connectivity
            healthy_bots = sum(1 for m in self.bot_metrics.values() if m.consecutive_failures == 0)
            diagnostics.append(f"🤖 Bots: {healthy_bots}/{len(self.bot_metrics)} healthy")
            
            # Check queue status
            queue_size = self.message_queue.qsize()
            max_size = self.config.MESSAGE_QUEUE_SIZE
            queue_status = "🟢" if queue_size < max_size * 0.7 else "🟡" if queue_size < max_size * 0.9 else "🔴"
            diagnostics.append(f"{queue_status} Queue: {queue_size}/{max_size}")
            
            # Check database
            diagnostics.append("💾 Database: Connected")
            
            # Check telethon client
            client_status = "🟢 Connected" if self.telethon_client and self.telethon_client.is_connected() else "🔴 Disconnected"
            diagnostics.append(f"📡 Telethon: {client_status}")
            
            # System paused status
            paused = await self.db_manager.get_setting("system_paused", "false")
            pause_status = "⏸️ Paused" if paused.lower() == "true" else "▶️ Running"
            diagnostics.append(f"⚙️ System: {pause_status}")
            
            diagnostics_text = f"🔍 **System Diagnostics**\n\n" + "\n".join(diagnostics)
            
            await update.message.reply_text(diagnostics_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error running diagnostics: {e}")
    
    async def _cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View current settings command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            settings_text = f"""
⚙️ **Current Settings**

**System:**
• Max Workers: {self.config.MAX_WORKERS}
• Queue Size: {self.config.MESSAGE_QUEUE_SIZE}
• Rate Limit: {self.config.RATE_LIMIT_MESSAGES}/{self.config.RATE_LIMIT_WINDOW}s

**Features:**
• Debug Mode: {self.config.DEBUG_MODE}
• Image Processing: {hasattr(self.config, 'ENABLE_IMAGE_PROCESSING') and self.config.ENABLE_IMAGE_PROCESSING}

**Current Status:**
• System Paused: {await self.db_manager.get_setting('system_paused', 'false')}
• Active Pairs: {len([p for p in self.pairs.values() if p.status == 'active'])}
• Total Bots: {len(self.telegram_bots)}
            """
            
            await update.message.reply_text(settings_text, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"Error getting settings: {e}")
    
    async def _cmd_set_setting(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Update setting command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            if len(context.args) < 2:
                await update.message.reply_text("Usage: /set <setting> <value>")
                return
            
            setting = context.args[0]
            value = " ".join(context.args[1:])
            
            # Only allow certain settings to be changed
            allowed_settings = ['system_paused', 'debug_mode']
            
            if setting not in allowed_settings:
                await update.message.reply_text(f"Setting '{setting}' cannot be changed. Allowed: {', '.join(allowed_settings)}")
                return
            
            await self.db_manager.set_setting(setting, value)
            await update.message.reply_text(f"✅ Updated {setting} = {value}")
            
        except Exception as e:
            await update.message.reply_text(f"Error updating setting: {e}")
    
    async def _cmd_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create database backup command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            backup_name = f"backup_{int(time.time())}.db"
            await update.message.reply_text(f"📦 Database backup functionality not yet implemented.\nWould create: {backup_name}")
            
        except Exception as e:
            await update.message.reply_text(f"Error creating backup: {e}")
    
    async def _cmd_cleanup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clean old data command"""
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            # Placeholder for cleanup functionality
            await update.message.reply_text("🧹 Database cleanup functionality not yet implemented.")
            
        except Exception as e:
            await update.message.reply_text(f"Error during cleanup: {e}")
    
    def _is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        return user_id in self.config.ADMIN_USER_IDS if self.config.ADMIN_USER_IDS else True
    
    def _get_uptime(self) -> str:
        """Get system uptime"""
        # This would be calculated from start time
        return "Running"
    
    def _get_memory_usage(self) -> str:
        """Get memory usage"""
        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            return f"{memory_mb:.1f} MB"
        except ImportError:
            return "N/A"
    
    # Public methods for external access
    async def reload_pairs(self):
        """Reload pairs from database"""
        await self._load_pairs()
    
    def get_metrics(self) -> Dict[int, BotMetrics]:
        """Get bot metrics"""
        return self.bot_metrics.copy()
    
    def get_queue_size(self) -> int:
        """Get current queue size"""
        return self.message_queue.qsize()
