import asyncio
import json
import re
import pandas as pd
import pywencai
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 忽略的无用字段
IGNORE_KEYS = ['txt1', 'txt2', 'code', 'url', 'wencai_data', 'meta', 'pid', 'logid', 'extra']


def _is_ignored(key: str) -> bool:
    """
    检查键名是否属于忽略列表
    """
    key_lower = str(key).lower()
    return any(ik in key_lower for ik in IGNORE_KEYS)


def clean_html(raw_html: str) -> str:
    """
    去除HTML标签，提取纯文本，并将特定标签转换为Markdown格式
    """
    if not isinstance(raw_html, str):
        return str(raw_html)

    # 将 <br>, <p>, <div> 转换为换行
    text = re.sub(r'<(br|p|div)[^>]*>', '\n', raw_html)
    # 去除其他 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)
    # 处理 HTML 实体
    text = text.replace('&nbsp;', ' ').replace('\u3000', ' ').replace('&gt;', '>').replace('&lt;', '<')
    # 去除多余的空行和首尾空白
    text = re.sub(r'\n\s*\n', '\n', text).strip()
    return text


def format_number(val):
    """
    格式化数字，将大数值转换为亿/万单位，保留两位小数
    """
    try:
        if isinstance(val, (int, float)):
            if abs(val) >= 1e8:
                return f"{val/1e8:.2f}亿"
            if abs(val) >= 1e4:
                return f"{val/1e4:.2f}万"
            return f"{val:.2f}"
        return val
    except Exception:
        return val


def format_value_recursive(val):
    """
    递归格式化数值
    """
    if isinstance(val, (int, float)):
        return format_number(val)
    if isinstance(val, dict):
        return {k: format_value_recursive(v) for k, v in val.items()}
    if isinstance(val, list):
        return [format_value_recursive(v) for v in val]
    return val


def format_dataframe(df: pd.DataFrame) -> str:
    """
    格式化 DataFrame 为 Markdown 表格
    """
    if df.empty:
        return ""

    # 复制一份副本以免修改原数据
    df_show = df.copy()

    # 尝试将所有数值列进行格式化
    for col in df_show.columns:
        # 如果列名包含“日期”，尝试转换日期格式
        if '日期' in str(col):
            try:
                df_show[col] = pd.to_datetime(df_show[col]).dt.strftime('%Y-%m-%d')
            except Exception:
                pass

        # 对数值进行格式化
        df_show[col] = df_show[col].apply(format_number)

    # 截断过长的列和行，以适应聊天窗口
    if len(df_show.columns) > 6:
        df_show = df_show.iloc[:, :6]
    if len(df_show) > 10:
        df_show = df_show.head(10)

    return df_show.to_markdown(index=False)


def format_dict_result(data: dict) -> str:
    """
    将字典格式的查询结果转换为易读的 Markdown 文本
    """
    result_parts = []

    # 优先处理 txt2 或 txt1 这种包含主要文本描述的字段
    main_text = data.get('txt2') or data.get('txt1')
    if main_text:
        result_parts.append(clean_html(main_text))
        result_parts.append("\n" + "-" * 20 + "\n")

    # 处理其他字段
    for key, value in data.items():
        if _is_ignored(key):
            continue

        if isinstance(value, dict):
            result_parts.append(f"**{key}**:")
            try:
                items = [{'项目': k, '数值': format_number(v)}
                         for k, v in value.items() if not _is_ignored(k)]
                if items:
                    result_parts.append(format_dataframe(pd.DataFrame(items)))
                else:
                    result_parts.append("(无有效数据)")
            except Exception:
                for sub_k, sub_v in value.items():
                    if not _is_ignored(sub_k):
                        result_parts.append(f"- {sub_k}: {format_number(sub_v)}")
            result_parts.append("")

        elif isinstance(value, list):
            if not value:
                continue
            result_parts.append(f"**{key}**:")
            if isinstance(value[0], dict):
                try:
                    cleaned_list = [{k: v for k, v in item.items() if not _is_ignored(k)}
                                    for item in value]
                    result_parts.append(format_dataframe(pd.DataFrame(cleaned_list)))
                except Exception:
                    for item in value:
                        result_parts.append(f"- {format_value_recursive(item)}")
            else:
                for item in value:
                    result_parts.append(f"- {format_value_recursive(item)}")
            result_parts.append("")

        elif isinstance(value, pd.DataFrame):
            result_parts.append(f"**{key}**:")
            result_parts.append(format_dataframe(value))
            result_parts.append("")

        else:
            if isinstance(value, str):
                if '<' in value and '>' in value:
                    value = clean_html(value)
                if "rows x" in value and "columns" in value:
                    continue
            result_parts.append(f"**{key}**: {format_number(value)}")

    return "\n".join(result_parts)


@register("wencai_plugin", "卢奇亚诺", "同花顺问财查询插件", "1.0.3")
class WencaiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("问财")
    async def wencai_query(self, event: AstrMessageEvent, query: str):
        """
        同花顺问财查询指令
        使用方法: /问财 <问题>
        示例: /问财 连续涨停板且未开板
        """
        if not query:
            yield event.plain_result("请输入查询内容，例如：/问财 连续涨停板")
            return

        yield event.plain_result(f"正在查询同花顺问财：{query}，请稍候...")

        try:
            # 使用运行中的事件循环
            loop = asyncio.get_running_loop()
            # 在线程池中运行 pywencai，避免阻塞主线程
            # pywencai 内部调用 Node.js 执行 JS
            # 移除 loop=True 以避免子线程环境下的事件循环冲突
            res = await loop.run_in_executor(None, lambda: pywencai.get(query=query))

            if res is None or (isinstance(res, pd.DataFrame) and res.empty):
                yield event.plain_result("未查询到相关数据。")
                return

            if isinstance(res, pd.DataFrame):
                markdown_table = format_dataframe(res)
                yield event.plain_result(f"查询结果：\n\n{markdown_table}")
            elif isinstance(res, dict):
                formatted_msg = format_dict_result(res)
                yield event.plain_result(f"查询结果：\n\n{formatted_msg}")
            else:
                yield event.plain_result(f"查询结果：\n{str(res)}")

        except Exception as e:
            error_msg = str(e)
            if "node" in error_msg.lower():
                yield event.plain_result("查询失败：未检测到 Node.js 环境，请联系管理员安装 Node.js。")
            else:
                logger.error(f"问财查询出错: {e}")
                yield event.plain_result(f"查询出错: {error_msg}")
