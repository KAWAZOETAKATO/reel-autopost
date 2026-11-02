"""Notion API（公式・2022-06-28版）を最小限だけ使う小さなクライアント。"""
from __future__ import annotations

import json

from .common import http_json

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


class Notion:
    def __init__(self, token: str, http=http_json):
        self._token = token
        self._http = http

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Notion-Version": VERSION}

    def query(self, db_id: str, filter_: dict | None = None, sorts: list | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            body: dict = {"page_size": 100}
            if filter_:
                body["filter"] = filter_
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            r = self._http("POST", f"{API}/databases/{db_id}/query", headers=self._h(), body=body)
            out += r.get("results", [])
            if not r.get("has_more"):
                return out
            cursor = r.get("next_cursor")

    def update(self, page_id: str, props: dict) -> dict:
        return self._http("PATCH", f"{API}/pages/{page_id}", headers=self._h(), body={"properties": props})

    def create(self, db_id: str, props: dict) -> dict:
        return self._http("POST", f"{API}/pages", headers=self._h(),
                          body={"parent": {"database_id": db_id}, "properties": props})

    def script_json(self, page_id: str) -> dict:
        """ページ本文の最初のコードブロック（台本JSON）を読み出す。"""
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self._http("GET", f"{API}/blocks/{page_id}/children", headers=self._h(), params=params)
            for b in r.get("results", []):
                if b.get("type") == "code":
                    text = "".join(t.get("plain_text", "") for t in b["code"].get("rich_text", []))
                    return json.loads(text)
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        raise ValueError("ページ本文に台本JSONのコードブロックがありません")


# ---- プロパティの読み書きヘルパー -------------------------------------------
def p_title(page: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in page["properties"].get(name, {}).get("title", []))


def p_text(page: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in page["properties"].get(name, {}).get("rich_text", []))


def p_select(page: dict, name: str) -> str:
    s = page["properties"].get(name, {}).get("select")
    return s["name"] if s else ""


def p_status(page: dict, name: str) -> str:
    s = page["properties"].get(name, {}).get("status")
    return s["name"] if s else ""


def p_date(page: dict, name: str) -> str:
    d = page["properties"].get(name, {}).get("date")
    return d["start"] if d else ""


def w_status(v: str) -> dict:
    return {"status": {"name": v}}


def w_text(v: str) -> dict:
    return {"rich_text": [{"text": {"content": v[:1900]}}]}


def w_title(v: str) -> dict:
    return {"title": [{"text": {"content": v[:1900]}}]}


def w_date(v: str) -> dict:
    return {"date": {"start": v}}


def w_select(v: str) -> dict:
    return {"select": {"name": v}}
