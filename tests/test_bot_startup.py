import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class BotStartupTest(unittest.TestCase):
    def test_bot_registers_plugins_without_connecting(self):
        # Use an isolated interpreter because bot.py configures process-wide logging.
        # No production credentials or database configuration are needed for this test.
        script = textwrap.dedent('''
            from unittest.mock import patch

            import dns.resolver  # Required for mongodb+srv connection strings.
            import psycopg2     # Also ship the optional PostgreSQL driver.

            with patch("logging.config.fileConfig"):
                from bot import Bot

            bot = Bot()
            with patch.object(bot, "add_handler") as add_handler:
                bot.load_plugins()
                assert add_handler.called, "no Telegram plugins registered their handlers"
        ''')
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **{key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ},
                'PYTHONDONTWRITEBYTECODE': '1',
                'API_ID': '12345',
                'DATABASE_URI': 'mongodb://127.0.0.1:27017/?connect=false',
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_result_links_and_delivery_on_both_pages(self):
        script = textwrap.dedent('''
            import asyncio
            from types import SimpleNamespace as NS
            from unittest.mock import AsyncMock, patch

            from pyrogram import enums
            from plugins import commands, pm_filter

            async def check():
                file_id = 'BAADAgADt3oABi5qAAYWBA'
                media = NS(file_id=file_id, file_name='Example', file_size=100, caption=None)
                expected_url = f'https://t.me/example_bot?start=filep_{file_id}'
                user = NS(id=7, mention='User')
                chat = NS(id=-100123, type=enums.ChatType.SUPERGROUP)
                message = NS(id=42, chat=chat, from_user=user, text='example',
                             reply_text=AsyncMock(return_value=NS(delete=AsyncMock())))
                query = NS(data='next_7_-100123-42_10', from_user=user, message=message,
                           edit_message_reply_markup=AsyncMock(), answer=AsyncMock())
                settings = dict(button=True, file_secure=True, imdb=False, template='')
                with patch.object(pm_filter, 'HYPER_MODE', False), \\
                     patch.object(pm_filter.temp, 'U_NAME', 'example_bot'), \\
                     patch.object(pm_filter, 'get_settings', AsyncMock(return_value=settings)), \\
                     patch.object(pm_filter, 'get_search_results', AsyncMock(return_value=([media], 10, 11, 0.001))), \\
                     patch.object(pm_filter.asyncio, 'sleep', AsyncMock()):
                    await pm_filter.auto_filter(None, message)
                    rows = message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
                    assert rows[0][0].url == expected_url and rows[0][0].callback_data is None
                    assert rows[1][-1].callback_data == query.data
                    settings['button'] = False
                    await pm_filter.next_page(None, query)
                    rows = query.edit_message_reply_markup.await_args.kwargs['reply_markup'].inline_keyboard
                    assert len(rows[0]) == 2 and all(b.url == expected_url for b in rows[0])
                    assert rows[1][0].callback_data == 'next_7_-100123-42_0'
                    with patch.object(pm_filter, 'HYPER_MODE', True):
                        caption, buttons = pm_filter._file_results([media], settings, separator='--')
                        assert expected_url in caption and not buttons
                    settings['file_secure'] = False
                    _, buttons = pm_filter._file_results([media], settings, separator='--')
                    assert buttons[0][0].url == f'https://t.me/example_bot?start=file_{file_id}'

                pm_message = NS(id=44, chat=NS(id=7, type=enums.ChatType.PRIVATE),
                                from_user=user, forward_origin=None, text='example',
                                reply_text=AsyncMock(return_value=NS(delete=AsyncMock())))
                assert await pm_filter._search_message_filter(None, None, pm_message)
                for special in ['/start', 'https://t.me/example/123']:
                    assert not await pm_filter._search_message_filter(None, None, NS(
                        text=special, chat=pm_message.chat, from_user=user, forward_origin=None,
                    ))
                pm_message.forward_origin = object()
                assert not await pm_filter._search_message_filter(None, None, pm_message)
                pm_message.forward_origin = None
                with patch.object(pm_filter, 'PM_SEARCH_ENABLED', True), \\
                     patch.object(pm_filter, 'AUTH_USERS', [user.id]), \\
                     patch.object(pm_filter, 'HYPER_MODE', False), \\
                     patch.object(pm_filter.temp, 'U_NAME', 'example_bot'), \\
                     patch.dict(pm_filter._PM_SEARCH_SETTINGS, settings, clear=False), \\
                     patch.object(pm_filter, 'get_settings', AsyncMock()) as group_settings, \\
                     patch.object(pm_filter, '_expire_pm_result', AsyncMock()), \\
                     patch.object(pm_filter, 'is_subscribed', AsyncMock(side_effect=[False, True])), \\
                     patch.object(pm_filter, 'create_invite_links', AsyncMock(return_value={1: 'https://t.me/updates'})), \\
                     patch.object(pm_filter, 'get_search_results', AsyncMock(return_value=([media], 10, 11, 0.001))) as search:
                    await pm_filter.give_filter(None, pm_message)
                    rows = pm_message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
                    assert rows[0][0].url == 'https://t.me/updates'
                    search.assert_not_awaited()
                    await pm_filter.give_filter(None, pm_message)
                    rows = pm_message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
                    assert rows[0][0].url == f'https://t.me/example_bot?start=file_{file_id}'
                    assert rows[1][-1].callback_data == 'next_7_7-44_10'
                    pm_query = NS(data=rows[1][-1].callback_data, message=pm_message,
                                  from_user=user, edit_message_reply_markup=AsyncMock(), answer=AsyncMock())
                    await pm_filter.next_page(None, pm_query)
                    assert search.await_args.kwargs['offset'] == 10
                    assert pm_query.edit_message_reply_markup.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url
                    assert search.await_count == 2
                    group_settings.assert_not_awaited()
                    with patch.object(pm_filter, 'AUTH_USERS', [99]):
                        await pm_filter.give_filter(None, pm_message)
                        await pm_filter.next_page(None, pm_query)
                    assert search.await_count == 2
                    with patch.object(pm_filter.temp, 'BANNED_USERS', [user.id]):
                        assert not await pm_filter._search_message_filter(None, None, pm_message)
                    with patch.object(pm_filter, 'PM_SEARCH_ENABLED', False):
                        await pm_filter.give_filter(None, pm_message)
                    assert pm_message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url == pm_filter.PM_SEARCH_GROUP_LINK
                    assert search.await_count == 2

                start_message = NS(chat=NS(type=enums.ChatType.PRIVATE), from_user=user,
                                   command=['start', expected_url.split('=')[1]], reply=AsyncMock())
                client = NS(send_message=AsyncMock())
                with patch.object(commands.db, 'is_user_exist', AsyncMock(return_value=True)), \\
                     patch.object(commands, 'get_file_details', AsyncMock(return_value=[media])) as lookup, \\
                     patch.object(commands, 'create_invite_links', AsyncMock(return_value={1: 'https://t.me/updates'})), \\
                     patch.object(commands, 'is_subscribed', AsyncMock(side_effect=[False, True, True])), \\
                     patch.object(commands, 'send_file_to_user', AsyncMock()) as send:
                    await commands.start(client, start_message)
                    lookup.assert_not_awaited()
                    send.assert_not_awaited()
                    retry = client.send_message.await_args.kwargs['reply_markup'].inline_keyboard[-1][0]
                    assert retry.callback_data == f'checksubp#{file_id}'
                    await commands.start(client, start_message)
                    lookup.assert_awaited_once_with(file_id)
                    send.assert_awaited_once()
                    assert send.await_args.kwargs['file_id'] == file_id
                    assert send.await_args.kwargs['protect_content_flag'] is True
                    lookup.return_value = []
                    await commands.start(client, start_message)
                    start_message.reply.assert_awaited_once_with('No such file exist.')
                    send.assert_awaited_once()

            asyncio.run(check())
        ''')
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **{key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ},
                'PYTHONDONTWRITEBYTECODE': '1',
                'API_ID': '12345',
                'DATABASE_URI': 'mongodb://127.0.0.1:27017/?connect=false',
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
