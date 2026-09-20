"""Exercise the production handler against an isolated in-memory catalog."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from aiohttp import web
from sqlalchemy import Column, Integer, String, select, func as sa_func, event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Content(Base):
    __tablename__ = 'artist_content'
    id = Column(Integer, primary_key=True)
    content_type = Column(String)
    artist_name = Column(String)
    views = Column(Integer)


class ShortsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')

        @event.listens_for(self.engine.sync_engine, 'connect')
        def md5(connection, _):
            connection.create_function('md5', 1, lambda value: hashlib.md5(value.encode()).hexdigest())

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all([Content(id=i, content_type='short', artist_name=f'Artist {i % 3}', views=i % 17)
                             for i in range(1, 251)])
            session.add(Content(id=900, content_type='video', artist_name='Other', views=999))
            await session.commit()
        source = ast.parse((Path(__file__).parents[1] / 'webhook.py').read_text(encoding='utf-8-sig'))
        handler = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'api_get_shorts')
        namespace = dict(web=web, select=select, sa_func=sa_func, ArtistContent=Content,
                         async_session=self.sessions, _content_meta=lambda row: {'id': row.id, 'artist_name': row.artist_name})
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'webhook.py', 'exec'), namespace)
        self.handler = namespace['api_get_shorts']

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def request(self, **params):
        response = await self.handler(SimpleNamespace(query={key: str(value) for key, value in params.items()}))
        return response.status, json.loads(response.text)

    async def test_legacy_newest_and_limits(self):
        status, data = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual([row['id'] for row in data['shorts']], list(range(250, 226, -1)))
        self.assertEqual(len((await self.request(limit=1000))[1]['shorts']), 100)

    async def test_shuffle_spans_catalog_and_is_seeded(self):
        data = (await self.request(order='random', seed=42))[1]
        same = (await self.request(order='random', seed=42))[1]
        different = (await self.request(order='random', seed=43))[1]
        self.assertEqual(data['shorts'], same['shorts'])
        self.assertNotEqual(data['shorts'], different['shorts'])
        self.assertLess(min(row['id'] for row in data['shorts']), 125)
        self.assertTrue(all(row['id'] <= 250 for row in data['shorts']))

    async def test_pages_cover_snapshot_once_despite_new_upload(self):
        page = (await self.request(order='random', seed=42))[1]
        ids = [row['id'] for row in page['shorts']]
        async with self.sessions() as session:
            session.add(Content(id=901, content_type='short', artist_name='New', views=0))
            await session.commit()
        while page['has_more']:
            page = (await self.request(order='random', seed=42, offset=page['next_offset'], max_id=page['max_id']))[1]
            ids.extend(row['id'] for row in page['shorts'])
        self.assertEqual(len(ids), 250)
        self.assertEqual(set(ids), set(range(1, 251)))

    async def test_filters_and_invalid_input(self):
        data = (await self.request(order='random', seed=5, artist='Artist 2'))[1]
        self.assertTrue(all(row['artist_name'] == 'Artist 2' for row in data['shorts']))
        best = (await self.request(order='best', limit=5))[1]
        self.assertTrue(all(row['id'] % 17 == 16 for row in best['shorts']))
        for params in [{'limit': 'oops'}, {'offset': 'bad'}, {'seed': "1' OR TRUE"}, {'max_id': 'x'}]:
            self.assertEqual((await self.request(**params))[0], 400)
        empty = (await self.request(artist='Missing'))[1]
        self.assertFalse(empty['has_more'])
        self.assertEqual(empty['shorts'], [])


if __name__ == '__main__':
    unittest.main()
