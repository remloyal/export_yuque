import json
import asyncio
import os
import re
import sys
import urllib.parse
from html.parser import HTMLParser
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional

import aiohttp

from ..libs.constants import GLOBAL_CONFIG
from ..libs.encrypt import encrypt_password
from ..libs.log import Log
from ..libs.request import Request
from ..libs.tools import (
    get_cache_user_info, is_personal,
    save_user_info, save_books_info
)

try:
    from ..libs.tools import get_local_cookies
except Exception:
    get_local_cookies = None

# 导入调试日志模块
try:
    from ..libs.debug_logger import DebugLogger

    _has_debug_logger = True
except ImportError:
    _has_debug_logger = False


class YuqueApi:
    """语雀API类"""

    _pw = None
    _pw_browser = None
    _pw_context = None
    _pw_lock = None
    _pw_semaphore = None
    _pw_loop = None
    _pw_semaphore_size = None

    @staticmethod
    def _ensure_playwright_bound_to_current_loop() -> None:
        """确保 Playwright 复用状态绑定到当前 event loop。

        AsyncWorker 每次导出都会创建一个新的 event loop（并在结束后 close）。
        任何在旧 loop 创建的 asyncio.Lock/Semaphore 以及 Playwright 对象都不能在新 loop 继续使用，
        否则会出现："is bound to a different event loop"。
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if YuqueApi._pw_loop is None:
            YuqueApi._pw_loop = current_loop
            return

        if YuqueApi._pw_loop is not current_loop:
            # 不尝试在新 loop 中关闭旧对象（旧 loop 可能已 close），直接丢弃引用并让后续重建。
            YuqueApi._pw = None
            YuqueApi._pw_browser = None
            YuqueApi._pw_context = None
            YuqueApi._pw_lock = None
            YuqueApi._pw_semaphore = None
            YuqueApi._pw_loop = current_loop
            YuqueApi._pw_semaphore_size = None

    @staticmethod
    async def _reset_playwright_state() -> None:
        """重置 Playwright 复用状态。

        Playwright/Browser/Context 在异常情况下可能进入不可用状态（例如内部 connection 为 None），
        这里做一次尽力清理并置空，方便后续重新初始化。
        """
        if YuqueApi._pw_lock is None:
            YuqueApi._pw_lock = asyncio.Lock()
        async with YuqueApi._pw_lock:
            try:
                if YuqueApi._pw_context is not None:
                    try:
                        await YuqueApi._pw_context.close()
                    except Exception:
                        pass
            finally:
                YuqueApi._pw_context = None

            try:
                if YuqueApi._pw_browser is not None:
                    try:
                        await YuqueApi._pw_browser.close()
                    except Exception:
                        pass
            finally:
                YuqueApi._pw_browser = None

            try:
                if YuqueApi._pw is not None:
                    try:
                        await YuqueApi._pw.stop()
                    except Exception:
                        pass
            finally:
                YuqueApi._pw = None

    class _NeViewerBodyExtractor(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self._capturing = False
            self._depth = 0
            self._parts: List[str] = []

        @property
        def html(self) -> str:
            return ''.join(self._parts).strip()

        def handle_starttag(self, tag, attrs):
            tag_l = tag.lower()
            if not self._capturing and tag_l == 'div':
                class_value = ''
                for k, v in attrs:
                    if k == 'class' and isinstance(v, str):
                        class_value = v
                        break
                classes = set(class_value.split()) if class_value else set()
                if 'ne-viewer-body' in classes:
                    self._capturing = True
                    self._depth = 1
                    return

            if self._capturing:
                self._parts.append(self.get_starttag_text() or f"<{tag}>")
                self._depth += 1

        def handle_startendtag(self, tag, attrs):
            if self._capturing:
                text = self.get_starttag_text()
                if text:
                    self._parts.append(text)
                else:
                    attrs_text = ''.join([f' {k}="{v}"' for k, v in attrs if v is not None])
                    self._parts.append(f"<{tag}{attrs_text} />")

        def handle_endtag(self, tag):
            if not self._capturing:
                return

            self._depth -= 1
            if self._depth == 0 and tag.lower() == 'div':
                self._capturing = False
                return

            self._parts.append(f"</{tag}>")

        def handle_data(self, data):
            if self._capturing and data:
                self._parts.append(data)

        def handle_entityref(self, name):
            if self._capturing:
                self._parts.append(f"&{name};")

        def handle_charref(self, name):
            if self._capturing:
                self._parts.append(f"&#{name};")

        def handle_comment(self, data):
            if self._capturing:
                self._parts.append(f"<!--{data}-->")

    @staticmethod
    def _normalize_doc_path(namespace: str, doc_identifier: str) -> Optional[str]:
        parts = namespace.split('/')
        if len(parts) != 2:
            return None
        user_login, repo_slug = parts

        if not doc_identifier:
            return None

        if doc_identifier.startswith('http://') or doc_identifier.startswith('https://'):
            try:
                return urlparse(doc_identifier).path
            except Exception:
                return None

        if doc_identifier.startswith('/'):
            return doc_identifier
        if doc_identifier.startswith(user_login + '/' + repo_slug):
            return '/' + doc_identifier
        if '/' in doc_identifier and not doc_identifier.startswith('/'):
            return f"/{doc_identifier}"
        return f"/{user_login}/{repo_slug}/{doc_identifier}"

    @staticmethod
    def _extract_ne_viewer_body_inner_html(page_html: str) -> Optional[str]:
        if not page_html or 'ne-viewer-body' not in page_html:
            return None
        parser = YuqueApi._NeViewerBodyExtractor()
        try:
            parser.feed(page_html)
            parser.close()
        except Exception:
            return None
        return parser.html or None

    @staticmethod
    def _wrap_html_document(body_inner_html: str, title: str = "") -> str:
        safe_title = (title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        base_href = Request._get_match_host().rstrip('/') + '/'  # type: ignore[attr-defined]
        # return (
        #     "<!doctype html>\n"
        #     "<html lang=\"zh-CN\">\n"
        #     "<head>\n"
        #     "  <meta charset=\"utf-8\">\n"
        #     "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        #     f"  <base href=\"{base_href}\">\n"
        #     f"  <title>{safe_title}</title>\n"
        #     "</head>\n"
        #     "<body>\n"
        #     f"{body_inner_html}\n"
        #     "</body>\n"
        #     "</html>\n"
        # )
        return (
            f"{body_inner_html}"
        )

    @staticmethod
    def _cookie_string_to_playwright_cookies(cookie_string: str, domain: str) -> List[Dict[str, Any]]:
        cookies: List[Dict[str, Any]] = []
        if not cookie_string:
            return cookies
        for part in cookie_string.split(';'):
            part = part.strip()
            if not part or '=' not in part:
                continue
            name, value = part.split('=', 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            cookies.append({
                'name': name,
                'value': value,
                'domain': domain,
                'path': '/',
            })
        return cookies

    @staticmethod
    async def _fetch_ne_viewer_body_via_playwright(full_url: str, playwright_concurrency: int = 1) -> Optional[str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None

        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

        # 关键：跨线程/跨 event loop 时必须重置复用状态
        YuqueApi._ensure_playwright_bound_to_current_loop()

        if YuqueApi._pw_lock is None:
            YuqueApi._pw_lock = asyncio.Lock()

        desired = max(1, int(playwright_concurrency or 1))
        if YuqueApi._pw_semaphore is None or YuqueApi._pw_semaphore_size != desired:
            # 避免并发打开太多页面（会非常慢且容易崩）
            YuqueApi._pw_semaphore = asyncio.Semaphore(desired)
            YuqueApi._pw_semaphore_size = desired

        cookie_string = ""
        if get_local_cookies is not None:
            try:
                cookie_string = get_local_cookies() or ""
            except Exception:
                cookie_string = ""

        domain = urlparse(full_url).hostname or ".yuque.com"
        pw_cookies = YuqueApi._cookie_string_to_playwright_cookies(cookie_string, domain=domain)

        async def _ensure_context():
            async with YuqueApi._pw_lock:
                # 启动Playwright
                if YuqueApi._pw is None:
                    YuqueApi._pw = await async_playwright().start()

                # 启动Browser（优先系统浏览器channel）
                if YuqueApi._pw_browser is None:
                    browser = None
                    for channel in ["msedge", "chrome", "360chrome", "qqbrowser", "brave"]:
                        try:
                            browser = await YuqueApi._pw.chromium.launch(headless=True, channel=channel)
                            break
                        except Exception:
                            continue
                    if browser is None:
                        try:
                            browser = await YuqueApi._pw.chromium.launch(headless=True)
                        except Exception as e:
                            msg = str(e)
                            if "playwright install" in msg or "Executable doesn't exist" in msg:
                                Log.warn("Playwright浏览器未安装：请先运行 `playwright install`（或安装系统Edge/Chrome以便使用channel启动）", detailed=True)
                            else:
                                Log.warn(f"Playwright启动失败: {msg}", detailed=True)
                            return None
                    YuqueApi._pw_browser = browser

                # 创建/复用Context
                if YuqueApi._pw_context is None:
                    try:
                        YuqueApi._pw_context = await YuqueApi._pw_browser.new_context()
                        if pw_cookies:
                            try:
                                await YuqueApi._pw_context.add_cookies(pw_cookies)
                            except Exception:
                                pass
                    except Exception:
                        YuqueApi._pw_context = None
                        return None

                return YuqueApi._pw_context

        async def _run_once() -> Optional[str]:
            context = await _ensure_context()
            if context is None:
                return None

            page = None
            try:
                page = await context.new_page()
                await page.goto(full_url, wait_until='networkidle', timeout=45000)
                await page.wait_for_selector('div.ne-viewer-body', timeout=20000)
                inner = await page.inner_html('div.ne-viewer-body')
                return inner.strip() if inner else None
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

        async with YuqueApi._pw_semaphore:
            try:
                return await _run_once()
            except Exception as e:
                # 典型失效：BrowserContext.new_page -> NoneType.send
                msg = str(e)
                if "NoneType" in msg and "send" in msg:
                    Log.warn("Playwright上下文疑似失效，正在重置并重试一次...", detailed=True)
                    await YuqueApi._reset_playwright_state()
                    try:
                        return await _run_once()
                    except Exception as e2:
                        Log.warn(f"Playwright重试仍失败: {str(e2)}", detailed=True)
                        return None
                raise

    @staticmethod
    async def _download_images_to_dir(
        html: str,
        doc_path: str,
        asset_dir: Optional[str],
        asset_url_prefix: str = "",
        concurrency: int = 4,
    ) -> str:
        """下载HTML中的图片到指定目录，并替换src为统一目录/文件名。

        Args:
            html: 待处理的HTML片段/文档
            doc_path: 文档路径，用于解析相对图片链接
            asset_dir: 图片保存的本地目录（绝对路径）。为空则不处理
            asset_url_prefix: 写回HTML时使用的前缀（如 "images/"）。
            concurrency: 并发下载图片数量限制
        """
        if not html or "<img" not in html.lower() or not asset_dir:
            return html

        img_pattern = re.compile(r"<img[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
        src_list: List[str] = []
        for match in img_pattern.finditer(html):
            src = match.group(1).strip()
            if not src or src.lower().startswith("data:"):
                continue
            if src not in src_list:
                src_list.append(src)

        if not src_list:
            return html

        os.makedirs(asset_dir, exist_ok=True)

        cookies = ""
        if get_local_cookies is not None:
            try:
                cookies = get_local_cookies() or ""
            except Exception:
                cookies = ""

        headers = Request._get_request_headers()
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        headers.pop("x-requested-with", None)
        if cookies:
            headers["cookie"] = cookies

        base_url = urllib.parse.urljoin(Request._get_match_host(), doc_path)

        semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
        replacements: Dict[str, str] = {}

        async with aiohttp.ClientSession() as session:
            async def fetch_and_save(src: str, idx: int) -> None:
                # 解析完整URL
                resolved = src
                if src.startswith("//"):
                    resolved = "https:" + src
                elif not urlparse(src).scheme:
                    resolved = urllib.parse.urljoin(base_url, src)

                # 如果同一src已处理过，复用结果
                if src in replacements:
                    return

                async with semaphore:
                    try:
                        async with session.get(resolved, headers=headers, timeout=30, ssl=False) as resp:
                            if resp.status != 200:
                                Log.warn(f"图片下载失败({resp.status}): {resolved}", detailed=True)
                                return
                            data = await resp.read()

                            # 生成文件名
                            parsed = urlparse(resolved)
                            filename = os.path.basename(parsed.path)
                            if not filename:
                                filename = f"image-{idx}"
                            name, ext = os.path.splitext(filename)
                            if not ext:
                                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                                guessed_ext = ""
                                if content_type == "image/png":
                                    guessed_ext = ".png"
                                elif content_type in ("image/jpeg", "image/jpg"):
                                    guessed_ext = ".jpg"
                                elif content_type == "image/gif":
                                    guessed_ext = ".gif"
                                elif content_type == "image/webp":
                                    guessed_ext = ".webp"
                                elif content_type == "image/svg+xml":
                                    guessed_ext = ".svg"
                                ext = guessed_ext or ".bin"
                            final_name = f"{name}{ext}"

                            # 防止重名覆盖
                            counter = 1
                            target_path = os.path.join(asset_dir, final_name)
                            while os.path.exists(target_path):
                                final_name = f"{name}-{counter}{ext}"
                                target_path = os.path.join(asset_dir, final_name)
                                counter += 1

                            with open(target_path, "wb") as f:
                                f.write(data)

                            prefix = asset_url_prefix or ""
                            if prefix and not prefix.endswith("/"):
                                prefix = prefix + "/"
                            replacements[src] = (prefix + final_name).replace("\\", "/")
                    except Exception as e:
                        Log.warn(f"图片下载异常: {resolved}: {str(e)}", detailed=True)

            await asyncio.gather(*(fetch_and_save(src, idx) for idx, src in enumerate(src_list)))

        if not replacements:
            return html

        replace_pattern = re.compile(r"(<img[^>]*\bsrc\s*=\s*)(['\"])([^'\"]+)(\2)", re.IGNORECASE)

        def _repl(match: re.Match) -> str:
            prefix, quote, src, _ = match.groups()
            new_src = replacements.get(src)
            if new_src:
                return f"{prefix}{quote}{new_src}{quote}"
            return match.group(0)

        return replace_pattern.sub(_repl, html)

    @staticmethod
    async def export_html(
        namespace: str,
        doc_identifier: str,
        title: str = "",
        playwright_concurrency: int = 1,
        asset_dir: Optional[str] = None,
        asset_url_prefix: str = "",
        asset_concurrency: int = 4,
        use_absolute_path: bool = False,
    ) -> Optional[str]:
        """导出文档为HTML（提取 div.ne-viewer-body 内部内容）

        策略：优先用 Request.get_text 抓取页面HTML并解析；失败再用 Playwright 渲染提取。
        """
        try:
            doc_path = YuqueApi._normalize_doc_path(namespace, doc_identifier)
            if not doc_path:
                Log.error(f"无效的知识库命名空间或文档标识符: {namespace}/{doc_identifier}")
                return None

            effective_asset_url_prefix = asset_url_prefix
            if use_absolute_path and asset_dir:
                # “绝对路径”语义：始终写成 images/xxx（其中 images 为 asset_dir 的目录名）
                effective_asset_url_prefix = os.path.basename(os.path.normpath(asset_dir))

            if _has_debug_logger:
                DebugLogger.log_info(f"导出HTML文档: {namespace}/{doc_identifier}")

            # 1) 直连抓取
            try:
                page_html = await Request.get_text(doc_path, is_html=True)
                inner = YuqueApi._extract_ne_viewer_body_inner_html(page_html)
                if inner:
                    html = YuqueApi._wrap_html_document(inner, title=title)
                    return await YuqueApi._download_images_to_dir(
                        html,
                        doc_path,
                        asset_dir,
                        effective_asset_url_prefix,
                        concurrency=asset_concurrency,
                    )
            except Exception as e:
                Log.warn(f"直连获取HTML失败，将尝试Playwright: {str(e)}", detailed=True)

            # 2) Playwright 渲染兜底
            try:
                full_url = urllib.parse.urljoin(Request._get_match_host(), doc_path)
                inner = await YuqueApi._fetch_ne_viewer_body_via_playwright(
                    full_url,
                    playwright_concurrency=playwright_concurrency,
                )
                if inner:
                    html = YuqueApi._wrap_html_document(inner, title=title)
                    return await YuqueApi._download_images_to_dir(
                        html,
                        doc_path,
                        asset_dir,
                        effective_asset_url_prefix,
                        concurrency=asset_concurrency,
                    )
            except Exception as e:
                Log.warn(f"Playwright获取HTML失败: {str(e)}", detailed=True)

            return None

        except Exception as e:
            Log.error(f"导出HTML失败: {str(e)}")
            if _has_debug_logger:
                DebugLogger.log_error(f"导出HTML异常: {str(e)}")
            return None

    @staticmethod
    async def login(username: str, password: str) -> bool:
        """登录语雀并存储cookies"""
        try:
            encrypted_password = encrypt_password(password)

            params = {
                "login": username,
                "password": encrypted_password,
                "loginType": "password"
            }

            if _has_debug_logger:
                # 不记录密码
                safe_params = params.copy()
                safe_params["password"] = "******"
                DebugLogger.log_info(f"尝试登录账号: {username}")
                DebugLogger.log_data("登录参数", safe_params)

            resp = await Request.post(GLOBAL_CONFIG.mobile_login, params)

            if _has_debug_logger:
                DebugLogger.log_data("登录响应", resp)

            if resp.get("data"):
                user_info = resp["data"]["me"]
                if save_user_info(user_info):
                    if _has_debug_logger:
                        safe_user_info = user_info.copy() if isinstance(user_info, dict) else user_info
                        DebugLogger.log_data("用户信息", safe_user_info)
                    return True
                else:
                    Log.error("缓存目录创建失败")
                    sys.exit(1)
            else:
                return False

        except Exception as e:
            Log.error(f"登录失败: {str(e)}")
            if _has_debug_logger:
                DebugLogger.log_error(f"登录过程发生异常: {str(e)}")
            return False

    @staticmethod
    async def get_user_info() -> bool:
        """获取当前登录用户信息并存储"""
        try:
            if _has_debug_logger:
                DebugLogger.log_info("开始获取用户信息")

            resp = await Request.get("/api/mine")

            if _has_debug_logger:
                DebugLogger.log_data("获取用户信息响应", resp)

            if resp.get("data"):
                user_info = resp["data"]
                if save_user_info(user_info):
                    if _has_debug_logger:
                        safe_user_info = user_info.copy() if isinstance(user_info, dict) else user_info
                        DebugLogger.log_data("用户信息", safe_user_info)
                    return True
                else:
                    Log.error("缓存目录创建失败")
                    return False
            else:
                Log.error("获取用户信息失败")
                return False

        except Exception as e:
            Log.error(f"获取用户信息失败: {str(e)}")
            if _has_debug_logger:
                DebugLogger.log_error(f"获取用户信息过程发生异常: {str(e)}")
            return False

    @staticmethod
    async def get_user_bookstacks() -> Optional[Dict[str, Any]]:
        """获取个人知识库/团队知识库列表数据"""
        try:
            personal = is_personal()
            Log.info("开始获取知识库")

            if _has_debug_logger:
                DebugLogger.log_info(f"获取知识库类型: {'个人' if personal else '团队'}")

            # 显示加载动画
            Log.info("正在获取知识库数据，请稍后...")

            target_api = GLOBAL_CONFIG.yuque_book_stacks if personal else GLOBAL_CONFIG.yuque_space_books_info

            if _has_debug_logger:
                DebugLogger.log_info(f"请求知识库API: {target_api}")

            resp = await Request.get(target_api)

            if _has_debug_logger:
                DebugLogger.log_data("知识库响应", resp)

            if resp.get("data"):
                data_wrap = resp["data"]

                if personal:
                    filtered_books_data = await YuqueApi._gen_books_data_for_cache(data_wrap)
                else:
                    # 构造一个 [{books:[...]}] 结构的数据
                    temp_books_data = [{"books": data_wrap}]
                    filtered_books_data = await YuqueApi._gen_books_data_for_cache(temp_books_data)

                if _has_debug_logger:
                    DebugLogger.log_data("过滤后的知识库数据", filtered_books_data)

                merged_books_data = []

                # 获取协作知识库
                try:
                    collab_books = await YuqueApi.get_collab_books()
                    if collab_books:
                        merged_books_data.extend(collab_books)
                        if _has_debug_logger:
                            DebugLogger.log_info(f"成功获取 {len(collab_books)} 个协作知识库")
                except Exception as e:
                    Log.warn(f"获取协作知识库失败: {str(e)}")
                    if _has_debug_logger:
                        DebugLogger.log_error(f"获取协作知识库异常: {str(e)}")

                # 添加主要知识库
                merged_books_data.extend(filtered_books_data)

                # 保存知识库信息
                if save_books_info(merged_books_data):
                    Log.success("知识库信息保存成功")
                    if _has_debug_logger:
                        DebugLogger.log_info(f"总共保存 {len(merged_books_data)} 个知识库")
                    return {"books_info": merged_books_data}
                else:
                    Log.error("文件创建失败")
                    sys.exit(1)
            else:
                Log.error("获取知识库数据失败")
                return None

        except Exception as e:
            Log.error(f"获取知识库失败: {str(e)}")
            if _has_debug_logger:
                DebugLogger.log_error(f"获取知识库异常: {str(e)}")
            return None

    @staticmethod
    async def _gen_books_data_for_cache(data_wrap: Any) -> List[Dict[str, Any]]:
        """生成用于缓存的知识库数据"""
        books_data = []

        try:
            if isinstance(data_wrap, list):
                # 处理团队知识库格式
                for group in data_wrap:
                    if "books" in group:
                        for book in group["books"]:
                            book_item = YuqueApi._format_book_item(book, "team")
                            books_data.append(book_item)
            else:
                # 处理个人知识库格式
                for book in data_wrap:
                    book_item = YuqueApi._format_book_item(book, "owner")
                    books_data.append(book_item)

        except Exception as e:
            Log.error(f"处理知识库数据失败: {str(e)}")

        return books_data

    @staticmethod
    def _format_book_item(book: Dict[str, Any], book_type: str) -> Dict[str, Any]:
        """格式化知识库项目"""
        return {
            "id": book.get("id", ""),
            "type": book.get("type", ""),
            "slug": book.get("slug", ""),
            "name": book.get("name", ""),
            "user_id": book.get("user_id", ""),
            "description": book.get("description", ""),
            "creator_id": book.get("creator_id", ""),
            "public": book.get("public", 0),
            "items_count": book.get("items_count", 0),
            "likes_count": book.get("likes_count", 0),
            "watches_count": book.get("watches_count", 0),
            "content_updated_at": book.get("content_updated_at", ""),
            "updated_at": book.get("updated_at", ""),
            "created_at": book.get("created_at", ""),
            "namespace": book.get("namespace", ""),
            "user": book.get("user", {}),
            "toc": book.get("toc", ""),
            "toc_yml": book.get("toc_yml", ""),
            "gitbook_token": book.get("gitbook_token", ""),
            "export_pdf_token": book.get("export_pdf_token", ""),
            "export_epub_token": book.get("export_epub_token", ""),
            "abilities": book.get("abilities", {}),
            "book_type": book_type,
            "docs": []
        }

    @staticmethod
    async def get_collab_books() -> Optional[List[Dict[str, Any]]]:
        """获取协作知识库"""
        try:
            resp = await Request.get(GLOBAL_CONFIG.yuque_collab_books_info)

            if resp.get("data"):
                collab_books = []
                for book in resp["data"]:
                    book_item = YuqueApi._format_book_item(book, "collab")
                    collab_books.append(book_item)
                return collab_books
            else:
                return []

        except Exception as e:
            Log.warn(f"获取协作知识库失败: {str(e)}")
            return []

    @staticmethod
    async def crawl_book_toc_info(url: str) -> Optional[Dict[str, Any]]:
        """爬取知识库页面获取目录信息"""
        try:
            # 打印调试信息
            Log.info(f"爬取页面 URL: {url}")

            try:
                text_content = await Request.get_text(url, is_html=True)
                Log.debug(f"页面内容长度: {len(text_content)}")
            except Exception as e:
                Log.error(f"获取页面内容失败: {str(e)}")
                return None

            # 正则表达式匹配
            patterns = [
                r'decodeURIComponent\("([^"]+)"\)'
            ]

            data = None
            for i, pattern in enumerate(patterns, 1):
                try:
                    matches = re.search(pattern, text_content, re.DOTALL)
                    if matches:

                        if "decodeURIComponent" in pattern:
                            # 需要URL解码
                            encoded_data = matches.group(1)
                            Log.debug(f"找到编码数据，长度: {len(encoded_data)}")
                            decoded_data = urllib.parse.unquote(encoded_data)
                            data = json.loads(decoded_data)
                        else:
                            # 直接是JSON字符串
                            json_str = matches.group(1)
                            Log.debug(f"找到JSON数据，长度: {len(json_str)}")
                            data = json.loads(json_str)

                        # 如果找到并解析成功，跳出循环
                        if data:
                            Log.debug(f"成功使用模式{i}解析知识库数据")
                            break
                except Exception as e:
                    Log.warn(f"模式{i}解析失败: {str(e)}", detailed=True)
                    continue

            if data:
                # 检查不同的数据结构格式
                if "book" in data and "toc" in data["book"]:
                    toc_count = len(data["book"]["toc"])
                    Log.debug(f"找到标准格式TOC，共 {toc_count} 个条目")
                    return data
                elif "toc" in data:
                    # 构造标准格式
                    toc_count = len(data["toc"])
                    Log.debug(f"找到替代格式TOC，共 {toc_count} 个条目")
                    return {"book": {"toc": data["toc"]}}
                elif "data" in data and "book" in data["data"]:
                    # API响应格式
                    Log.debug(f"找到API格式TOC")
                    if "toc" in data["data"]["book"]:
                        toc_data = data["data"]["book"]["toc"]
                        toc_count = len(toc_data)
                        Log.debug(f"共 {toc_count} 个条目")
                        return {"book": {"toc": toc_data}}

            Log.warn(f"无法在页面中找到知识库数据: {url}")

            # 打印页面内容的一部分以便于调试
            preview_length = min(500, len(text_content))
            Log.debug(f"页面内容预览: {text_content[:preview_length]}...")

            return None

        except Exception as e:
            Log.error(f"爬取知识库页面失败: {str(e)}")
            return None

    @staticmethod
    async def get_book_docs(namespace: str) -> Optional[List[Dict[str, Any]]]:
        """获取知识库中的文档列表"""
        try:
            # 构建URL
            url = f"/{namespace}"

            Log.debug(f"获取知识库文档列表: {namespace}")
            Log.debug(f"请求URL: {url}")

            # 获取HTML页面内容
            response_text = await Request.get_text(url, is_html=True)

            Log.debug(f"爬取页面内容长度: {len(response_text)}")

            # 先尝试从页面中提取TOC数据
            book_data = await YuqueApi.crawl_book_toc_info(url)

            if book_data and "book" in book_data and "toc" in book_data["book"]:
                toc_data = book_data["book"]["toc"]

                if _has_debug_logger:
                    DebugLogger.log_data("解析得到的TOC数据", toc_data)

                # 转换TOC数据为文档列表格式
                doc_list = []
                for item in toc_data:
                    # 提取slug，有些情况下可能需要从URL中提取
                    slug = item.get('slug', '')
                    url_path = item.get('url', '')

                    Log.debug(f"处理文档项: 标题: {item.get('title', '')}, 原始slug: {slug}, 原始URL: {url_path}")

                    # 尝试从各种可能的字段获取slug
                    if not slug and url_path:
                        # 尝试从URL中提取slug
                        slug_match = re.search(r'/([^/]+)$', url_path)
                        if slug_match:
                            slug = slug_match.group(1)
                            Log.debug(f"从URL提取到slug: {slug}")

                    # 尝试从doc_uuid或者uuid字段构建slug
                    if not slug and ('doc_uuid' in item or 'uuid' in item):
                        doc_uuid = item.get('doc_uuid', '') or item.get('uuid', '')
                        if doc_uuid:
                            slug = doc_uuid
                            Log.debug(f"使用UUID作为slug: {slug}")

                    # 尝试从标题生成slug
                    if not slug and 'title' in item:
                        title = item.get('title', '')
                        if title:
                            # 使用标题转换为URL安全的字符串作为slug
                            import hashlib
                            slug = hashlib.md5(title.encode('utf-8')).hexdigest()[:8]
                            Log.debug(f"从标题生成slug: {slug}")

                    doc = {
                        "id": item.get("id", ""),
                        "slug": slug,
                        "title": item.get("title", ""),
                        "url": url_path,  # 确保使用原始URL
                        "uuid": item.get("uuid", ""),
                        "type": item.get("type", "doc"),
                        "parent_uuid": item.get("parent_uuid", ""),
                        "level": item.get("level", 0),
                    }

                    if _has_debug_logger:
                        DebugLogger.log_data(f"处理后的文档项", doc)

                    doc_list.append(doc)

                return doc_list

            # 如果从页面提取失败，尝试从API获取
            Log.warn("从页面提取TOC失败，尝试从API获取", detailed=True)

            # 根据namespace构建API请求URL
            parts = namespace.split('/')
            if len(parts) != 2:
                Log.error(f"无效的知识库命名空间: {namespace}")
                return None

            api_url = f"/api/repos/{namespace}/toc"

            Log.debug(f"尝试从API获取TOC: {api_url}")

            try:
                api_response = await Request.get(api_url)

                if _has_debug_logger:
                    DebugLogger.log_data("API响应", api_response)

                if api_response and "data" in api_response:
                    raw_toc_data = api_response["data"]

                    Log.debug(f"API返回文档数量: {len(raw_toc_data)}")

                    # 转换API响应数据为所需格式
                    doc_list = []
                    for item in raw_toc_data:
                        doc = {
                            "id": item.get("id", ""),
                            "slug": item.get("slug", ""),
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "uuid": item.get("uuid", ""),
                            "type": item.get("type", "doc"),
                            "parent_uuid": item.get("parent_uuid", ""),
                            "level": item.get("level", 0),
                        }
                        doc_list.append(doc)

                    return doc_list
            except Exception as e:
                Log.error(f"API获取TOC失败: {str(e)}", detailed=True)
                if _has_debug_logger:
                    DebugLogger.log_error(f"API获取TOC异常: {str(e)}")

            Log.error(f"无法获取知识库 {namespace} 的文档列表")
            return None

        except Exception as e:
            Log.error(f"获取知识库文档列表失败: {str(e)}")
            if _has_debug_logger:
                DebugLogger.log_error(f"获取知识库文档列表异常: {str(e)}")
            return None

    @staticmethod
    async def get_doc_detail(namespace: str, slug: str) -> Optional[Dict[str, Any]]:
        """获取文档详情"""
        try:
            url = f"/api/repos/{namespace}/docs/{slug}"
            resp = await Request.get(url)

            if resp.get("data"):
                return resp["data"]
            else:
                Log.error(f"获取文档 {namespace}/{slug} 详情失败")
                return None

        except Exception as e:
            Log.error(f"获取文档详情失败: {str(e)}")
            return None

    @staticmethod
    async def export_markdown(namespace: str, doc_identifier: str, line_break: bool = True) -> Optional[str]:
        """导出文档为Markdown格式
        
        Args:
            namespace: 知识库命名空间，格式为 "user/repo"
            doc_identifier: 文档标识符，可以是完整URL路径或简单的slug
            line_break: 是否保留换行标识
        """
        try:
            if _has_debug_logger:
                DebugLogger.log_info(f"导出Markdown文档: {namespace}/{doc_identifier}")
                DebugLogger.log_info(
                    f"参数 - namespace: {namespace}, doc_identifier: {doc_identifier}, line_break: {line_break}")

            # 构建文档URL - 分解namespace以获取user和repo
            parts = namespace.split('/')
            if len(parts) != 2:
                Log.error(f"无效的知识库命名空间: {namespace}")
                if _has_debug_logger:
                    DebugLogger.log_error(f"无效的知识库命名空间: {namespace}")
                return None

            user_login, repo_slug = parts

            # 构建查询参数，与Rust版本保持一致
            query = f"attachment=true&latexcode=false&anchor=false&linebreak={str(line_break).lower()}"

            # 处理文档标识符 - 可能是URL路径或者slug
            target_doc_url = ""

            # 如果doc_identifier已经是完整的URL路径（如/xxx），直接使用
            if doc_identifier.startswith('/'):
                target_doc_url = doc_identifier
                Log.debug(f"使用完整URL路径: {target_doc_url}")

            # 如果包含完整的文档部分路径但缺少前导斜杠
            elif doc_identifier.startswith(user_login + '/' + repo_slug):
                target_doc_url = '/' + doc_identifier
                Log.debug(f"补全URL路径前导斜杠: {target_doc_url}")

            # 如果是相对路径但不以斜杠开头
            elif '/' in doc_identifier and not doc_identifier.startswith('/'):
                target_doc_url = f"/{doc_identifier}"
                Log.debug(f"转换相对路径为绝对路径: {target_doc_url}")

            # 如果只是简单的slug/标识符
            else:
                target_doc_url = f"/{user_login}/{repo_slug}/{doc_identifier}"
                Log.debug(f"构建完整URL: {target_doc_url}")

            # 构建完整的markdown导出URL
            markdown_url = f"{target_doc_url}/markdown?{query}"

            Log.debug(f"最终Markdown URL: {markdown_url}")

            try:
                resp = await Request.get_text(markdown_url)

                if _has_debug_logger:
                    preview = resp[:200] + "..." if len(resp) > 200 else resp
                    DebugLogger.log_info(f"Markdown内容长度: {len(resp)}")

                if resp and len(resp) > 10:  # 内容至少有一定长度才算有效
                    # 对内容进行后处理
                    # 1. 处理图片链接
                    resp = YuqueApi._process_image_links(resp)

                    # 2. 处理附件链接
                    resp = YuqueApi._process_attachment_links(resp)

                    return resp
                else:
                    Log.warn(f"获取到的Markdown内容可能为空: {markdown_url}", detailed=True)
                    if _has_debug_logger:
                        DebugLogger.log_warning(f"获取到的Markdown内容长度不足: {len(resp)}")

                    # 如果失败，尝试使用替代方法构建URL
                    if not target_doc_url.startswith(f"/{user_login}/{repo_slug}/"):
                        alt_url = f"/{user_login}/{repo_slug}/{doc_identifier}/markdown?{query}"

                        Log.debug(f"尝试替代URL: {alt_url}")

                        alt_resp = await Request.get_text(alt_url)

                        if _has_debug_logger:
                            alt_preview = alt_resp[:200] + "..." if len(alt_resp) > 200 else alt_resp
                            DebugLogger.log_info(f"替代URL响应长度: {len(alt_resp)}")
                            DebugLogger.log_data("替代URL响应预览", alt_preview)

                        if alt_resp and len(alt_resp) > 10:
                            # 处理图片和附件链接
                            alt_resp = YuqueApi._process_image_links(alt_resp)
                            alt_resp = YuqueApi._process_attachment_links(alt_resp)
                            return alt_resp
            except Exception as e:
                Log.warn(f"获取Markdown失败: {str(e)}", detailed=True)
                if _has_debug_logger:
                    DebugLogger.log_error(f"获取Markdown异常: {str(e)}")

            # 最后尝试API直接路径
            try:
                api_url = f"/api/docs/{namespace}/{doc_identifier}/markdown"

                Log.debug(f"尝试API直接路径: {api_url}")

                api_resp = await Request.get_text(api_url)

                if _has_debug_logger:
                    api_preview = api_resp[:200] + "..." if len(api_resp) > 200 else api_resp
                    DebugLogger.log_info(f"API路径响应长度: {len(api_resp)}")
                    DebugLogger.log_data("API路径响应预览", api_preview)

                if api_resp and len(api_resp) > 10:
                    # 处理图片和附件链接
                    api_resp = YuqueApi._process_image_links(api_resp)
                    api_resp = YuqueApi._process_attachment_links(api_resp)
                    return api_resp
            except Exception as e:
                Log.warn(f"API路径获取失败: {str(e)}", detailed=True)
                if _has_debug_logger:
                    DebugLogger.log_error(f"API路径获取异常: {str(e)}")

            Log.error(f"无法导出文档 {namespace}/{doc_identifier} 的Markdown内容")
            return "无法获取文档内容，可能文档类型不支持或文档已被删除或文档内容为空。"

        except Exception as e:
            Log.error(f"导出Markdown失败: {str(e)}")
            if _has_debug_logger:
                DebugLogger.log_error(f"导出Markdown异常: {str(e)}")
            return None

    @staticmethod
    async def download_attachment(url: str, file_path: str) -> bool:
        """下载附件"""
        try:
            return await Request.download_file(url, file_path)
        except Exception as e:
            Log.error(f"下载附件失败: {str(e)}")
            return False

    @staticmethod
    def _process_image_links(content: str) -> str:
        """处理Markdown中的图片链接，保留原始链接"""
        if not content:
            return content

        return content

    @staticmethod
    def _process_attachment_links(content: str) -> str:
        """处理Markdown中的附件链接，保留原始链接"""
        if not content:
            return content
        return content
