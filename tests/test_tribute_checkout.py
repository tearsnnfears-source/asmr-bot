"""Offline catalog and authenticated checkout checks; no payment is made."""
import ast
from contextlib import asynccontextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web

from utils.tribute_checkout import checkout_details, checkout_link, PRODUCTS

ROOT = Path(__file__).parents[1]


class CheckoutTests(unittest.IsolatedAsyncioTestCase):
    def test_all_seven_catalog_prices_and_new_links(self):
        with patch.dict(os.environ, {"TRIBUTE_CHECKOUT_ENABLED":"true","TRIBUTE_PLUS_URL":"https://example.test/old"}):
            for project,code,cents in [("asmrleaks","s1ajK",600),("privateleaks","s1ajL",550),
                                       ("asianleaks","s1ajN",525),("extraleaks","s1ajP",300)]:
                result=checkout_details(project,"plus")
                self.assertEqual((result["url"],result["price_cents"]),("https://t.me/tribute/app?startapp="+code,cents))
            self.assertEqual(checkout_details("asmrleaks","pro")["price_cents"],800)
            self.assertEqual(checkout_details("asmrleaks","elite","asianleaks")["price_cents"],1000)
            self.assertEqual(checkout_details("asmrleaks","king")["price_cents"],1300)

    def test_closed_default_and_invalid_plans_never_expose_checkout(self):
        with patch.dict(os.environ, {"TRIBUTE_CHECKOUT_ENABLED":"false"}):
            self.assertEqual(checkout_link("asmrleaks","plus"),"")
            with self.assertRaises(ValueError): checkout_details("asmrleaks","plus")
        with patch.dict(os.environ, {"TRIBUTE_CHECKOUT_ENABLED":"true"}):
            for args in [("privateleaks","pro"),("unknown","plus"),("asmrleaks","elite","asmrleaks"),
                         ("asianleaks","elite","asianleaks"),("asmrleaks","plus","asianleaks")]:
                with self.assertRaises(ValueError): checkout_details(*args)

    async def test_authenticated_id_and_server_prices_are_used_without_granting_access(self):
        prepare=AsyncMock(return_value=SimpleNamespace(id=91))
        @asynccontextmanager
        async def sessions(): yield object()
        ns=dict(web=web,parse_init_data=AsyncMock(return_value={"user_id":77}),
                checkout_details=checkout_details,PROJECT_KEY="asmrleaks",
                async_session=sessions,prepare_bundle_checkout=prepare,
                logger=SimpleNamespace(error=lambda *args:None))
        tree=ast.parse((ROOT/"webhook.py").read_text(encoding="utf-8-sig"))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="api_tribute_checkout")
        exec(compile(ast.Module(body=[node],type_ignores=[]),"webhook.py","exec"),ns)
        request=SimpleNamespace(json=AsyncMock(return_value={"tier":"plus","telegram_id":999,"price":1}))
        with patch.dict(os.environ, {"TRIBUTE_CHECKOUT_ENABLED":"true"}):
            response=await ns["api_tribute_checkout"](request)
            self.assertEqual(response.status,200)
            self.assertIn(b'"price_cents": 600',response.body)
            prepare.assert_not_awaited()
            request.json.return_value={"tier":"elite","bundle_target":"asianleaks","telegram_id":999}
            self.assertEqual((await ns["api_tribute_checkout"](request)).status,200)
            self.assertEqual(prepare.await_args.kwargs["telegram_id"],77)
            ns["parse_init_data"].return_value=None
            self.assertEqual((await ns["api_tribute_checkout"](request)).status,401)
