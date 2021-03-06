import os
from abc import ABC, abstractmethod
import threading
import logging
import logging.config
import asyncio
import traceback
import sys
import importlib
import uuid
from pathlib import Path
import io

from responder3.core.commons import *


class LogExtensionTaskEntry:
	def __init__(self, taskid):
		self.taskid = taskid
		self.created_at = datetime.datetime.utcnow()
		self.started_at = None
		self.extension_config = None
		self.extension_coro = None
		self.extension_handler = None
		self.extension_log_queue = asyncio.Queue()
		self.extension_command_queue = asyncio.Queue()


class LogProcessor:
	def __init__(self, log_settings, log_queue, loop = None):
		"""
		Extensible logging process. Does the logging via python's built-in logging module.
		:param logsettings: Dictionary describing the logging settings
		:type logsettings: dict
		:param logQ: Queue to read logging messages from
		:type logQ: multiprocessing.Queue
		"""
		self.loop = loop if loop is not None else asyncio.get_event_loop()
		self.log_settings = log_settings
		self.log_queue = log_queue
		self.logger = None
		self.extensions_tasks = {}
		self.extension_task_id = 0
		self.result_history = {}
		self.proxy_file_handler = None
		self.name = 'LogProcessor'

	async def log(self, message, level=logging.INFO):
		"""
		Logging function used to send logs in this process only!
		:param message: The message to be logged
		:type message: str
		:param level: log level
		:type level: int
		:return: None
		"""
		await self.handle_log(LogEntry(level, self.name, message))

	async def start_extension(self, handler, extension_config):
		try:
			let = LogExtensionTaskEntry(self.extension_task_id)
			self.extension_task_id += 1
			let.extension_config = extension_config
			let.extension_handler = handler(
				self.log_queue,
				let.extension_log_queue,
				let.extension_command_queue,
				let.extension_config,
				self.loop
			)
			let.extension_coro = let.extension_handler.create_coro()
			self.extensions_tasks[let.taskid] = let
			self.loop.create_task(let.extension_coro)
		except Exception as e:
			await self.log_exception()

		return

	def create_dir_strucutre(self):
		if 'logdir' in self.log_settings:
			Path(self.log_settings['logdir']).mkdir(parents=True, exist_ok=True)
			Path(self.log_settings['logdir'], 'emails').mkdir(parents=True, exist_ok=True)
			Path(self.log_settings['logdir'], 'proxydata').mkdir(parents=True, exist_ok=True)
			Path(self.log_settings['logdir'], 'poisondata').mkdir(parents=True, exist_ok=True)
			Path(self.log_settings['logdir'], 'creds').mkdir(parents=True, exist_ok=True)

	def get_handlers(self):
		for handler in self.log_settings['handlers']:
			if handler == 'TEST':
				handlerclass = TestExtension
				handlerclassname = 'TEST'
			else:
				handlerclassname = '%sHandler' % self.log_settings['handlers'][handler]
				handlermodulename = 'responder3_log_%s' % handler.replace('-', '_').lower()
				handlermodulename = '%s.%s' % (handlermodulename, handlerclassname)
				self.log('Importing handler module: %s , %s' % (handlermodulename, handlerclassname), logging.DEBUG)
				handlerclass = getattr(importlib.import_module(handlermodulename), handlerclassname)

			yield handlerclass, handler

	async def setup(self):
		"""
		Parses the settings dict and populates the necessary variables
		:return: None
		"""
		logging.config.dictConfig(self.log_settings['log'])
		self.logger = logging.getLogger('Responder3')
		self.create_dir_strucutre()

		if 'handlers' in self.log_settings:
			for handlerclass, handler in self.get_handlers():
				await self.start_extension(handlerclass, self.log_settings[self.log_settings['handlers'][handler]])

	async def run(self):
		try:
			await self.setup()
			await self.log('setup done', logging.DEBUG)
			while True:
				result = await self.log_queue.get()
				for taskid in self.extensions_tasks:
					await self.extensions_tasks[taskid].extension_log_queue.put(result)
				if isinstance(result, Credential):
					await self.handle_credential(result)
				elif isinstance(result, LogEntry):
					await self.handle_log(result)
				elif isinstance(result, Connection):
					await self.handle_connection(result)
				elif isinstance(result, EmailEntry):
					await self.handle_email(result)
				elif isinstance(result, PoisonResult):
					await self.handle_poisonresult(result)
				elif isinstance(result, ProxyData):
					await self.handle_proxydata(result)
				else:
					raise Exception('Unknown object in queue! Got type: %s' % type(result))

		except Exception as e:
			await self.log_exception('Logger main task exception')

		finally:
			if self.proxy_file_handler is not None:
				self.proxy_file_handler.close()

	async def handle_log(self, log):
		"""
		Handles the messages of log type
		:param log: Log message object
		:type log: LogEntry
		:return: None
		"""
		self.logger.log(log.level, str(log))

	def handle_connection(self, con):
		"""
		Handles the messages of log type
		:param con: Connection message object
		:type con: Connection
		:return: None
		"""
		self.logger.log(logging.INFO, str(con))

	async def handle_credential(self, result):
		"""
		Logs credential object arriving from logqueue
		:param result: Credential object to log
		:type result: Credential
		:return: None
		"""
		if 'logdir' in self.log_settings:
			filename = 'cred_%s_%s.json' % (datetime.datetime.utcnow().isoformat(), str(uuid.uuid4()))
			with open(str(Path(self.log_settings['logdir'], 'creds', filename).resolve()), 'wb') as f:
				f.write(result.to_json())

		if result.fingerprint not in self.result_history:
			await self.log(str(result.to_dict()), logging.INFO)
			self.result_history[result.fingerprint] = result
		else:
			await self.log('Duplicate result found! Filtered.')

	async def handle_email(self, email):
		"""
		Logs the email object arriving from logqueue
		:param email: Email object to log
		:type email: Email
		:return:
		"""
		if 'logdir' in self.log_settings:
			filename = 'email_%s_%s.eml' % (datetime.datetime.utcnow().isoformat(), str(uuid.uuid4()))
			with open(str(Path(self.log_settings['logdir'], 'emails', filename).resolve()), 'wb') as f:
				f.write(email.email.as_bytes())

		await self.log('You got mail!')

	async def handle_poisonresult(self, poisonresult):
		"""
		Logs the poisonresult object arriving from logqueue
		:param poisonresult:
		:type poisonresult: PoisonResult
		:return: None
		"""
		if 'logdir' in self.log_settings:
			filename = 'pr_%s_%s.json' % (datetime.datetime.utcnow().isoformat(), str(uuid.uuid4()))
			with open(str(Path(self.log_settings['logdir'], 'poisondata', filename).resolve()), 'wb') as f:
				f.write(poisonresult.to_json())
		await self.log(repr(poisonresult))

	async def handle_proxydata(self, proxydata):
		# TODO: currently it flushes everything on each line, this is not good (slow)
		# need to write a better scheduler for outout, timer maybe?
		"""
		Writes the incoming proxydata to a file
		:param proxydata: ProxyData
		:type proxydata: ProxyData
		:return: None
		"""
		if 'logdir' in self.log_settings and self.proxy_file_handler is None:
			filename = 'pr_%s_%s.json' % (datetime.datetime.utcnow().isoformat(), str(uuid.uuid4()))
			self.proxy_file_handler = open(str(Path(self.log_settings['logdir'], 'proxydata', filename).resolve()), 'wb')

		if self.proxy_file_handler is not None:
			try:
				self.proxy_file_handler.write(proxydata.to_json().encode() + b'\r\n')
				self.proxy_file_handler.flush()
				os.fsync(self.proxy_file_handler.fileno())
			except Exception as e:
				self.log_exception('Error writing proxy data to file!')
				return
		await self.log(repr(proxydata), logging.DEBUG)

	# this function is a duplicate, clean it up!
	async def log_exception(self, message=None):
		"""
		Custom exception handler to log exceptions via the logging interface
		:param message: Extra message for the exception if any
		:type message: str
		:return: None
		"""
		sio = io.StringIO()
		ei = sys.exc_info()
		tb = ei[2]
		traceback.print_exception(ei[0], ei[1], tb, None, sio)
		msg = sio.getvalue()
		if msg[-1] == '\n':
			msg = msg[:-1]
		sio.close()
		if message is not None:
			msg = message + msg
		await self.log(msg, level=logging.ERROR)


class LoggerExtensionTask(ABC):
	def __init__(self, log_queue, result_queue, command_queue, config, loop):
		self.result_queue = result_queue
		self.log_queue = log_queue
		self.command_queue = command_queue
		self.loop = loop
		self.config = config
		self.modulename = '%s-%s' % ('LogExt', self.__class__.__name__)
		self.init()

	async def run(self):
		try:
			await self.setup()
			await self.log('Started!', logging.DEBUG)
			await self.main()
			await self.log('Exiting!', logging.DEBUG)
		except Exception:
			await self.log_exception('Exception in main function!')

	@abstractmethod
	def init(self):
		pass

	@abstractmethod
	async def main(self):
		pass

	@abstractmethod
	async def setup(self):
		pass

	async def log_exception(self, message=None):
		"""
		Custom exception handler to log exceptions via the logging interface
		:param message: Extra message for the exception if any
		:type message: str
		:return: None
		"""
		sio = io.StringIO()
		ei = sys.exc_info()
		tb = ei[2]
		traceback.print_exception(ei[0], ei[1], tb, None, sio)
		msg = sio.getvalue()
		if msg[-1] == '\n':
			msg = msg[:-1]
		sio.close()
		if message is not None:
			msg = message + msg
		await self.log(msg, level=logging.ERROR)

	async def log(self, message, level=logging.INFO):
		"""
		Sends the log messages onto the logqueue. If no logqueue is present then prints them out on console.
		:param message: The message to be sent
		:type message: str
		:param level: Log level
		:type level: int
		:return: None
		"""
		if self.log_queue is not None:
			await self.log_queue.put(LogEntry(level, self.modulename, message))
		else:
			print(str(LogEntry(level, self.modulename, message)))


class TestExtension(LoggerExtensionTask):
	def init(self):
		self.output_queue = self.config['output_queue']

	async def setup(self):
		pass

	async def create_coro(self):
		return await self.main()

	async def main(self):
		try:
			while True:
				result = await self.result_queue.get()
				self.output_queue.put(result)
		except Exception as e:
			await self.log_exception()


