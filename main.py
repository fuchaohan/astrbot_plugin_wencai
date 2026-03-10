from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import pandas as pd
import pywencai
import logging
import json
import asyncio
import re

# 配置日志
logger = logging.getLogger("astrbot")

def clean_html(raw_html):
    """
    去除HTML标签，提取纯文本，并将特定标签转换为Markdown格式
    """
    if not isinstance(raw_html, str):
        return raw_html
    
    # 将 <br>, <p>, <div> 转换为换行
    text = re.sub(r'<(br|p|div)[^>]*>', '\n', raw_html)
    # 去除其他 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)
    # 处理 HTML 实体
    text = text.replace('&nbsp;', ' ').replace('\u3000', ' ').replace('&gt;', '>').replace('&lt;', '<')
    # 去除多余的空行 and 首尾空白
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
            elif abs(val) >= 1e4:
                return f"{val/1e4:.2f}万"
            else:
                return f"{val:.2f}"
        return val
    except:
        return val

def format_dataframe(df: pd.DataFrame) -> str:
    """
    格式化 DataFrame 为 Markdown 表格
    """
    # 复制一份副本以免修改原数据
    df_show = df.copy()
    
    # 尝试将所有数值列进行格式化
    for col in df_show.columns:
        # 如果列名包含“日期”，尝试转换日期格式
        if '日期' in str(col):
             try:
                df_show[col] = pd.to_datetime(df_show[col]).dt.strftime('%Y-%m-%d')
             except:
                 pass
        
        # 对数值进行格式化
        df_show[col] = df_show[col].apply(format_number)

    # 截断过长的列和行
    if len(df_show.columns) > 8:
        df_show = df_show.iloc[:, :8]
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
        result_parts.append("\n" + "-"*20 + "\n") # 分隔符

    # 处理其他字段
    for key, value in data.items():
        if key in ['txt1', 'txt2', 'code', 'url', 'wencai_data']: # 跳过已处理或不需要的字段
            continue
            
        if isinstance(value, dict):
            # 处理嵌套字典，例如 '拓维信息主营收入构成'
            result_parts.append(f"**{key}**:")
            # 将嵌套字典转换为 Markdown 表格
            try:
                sub_df = pd.DataFrame(list(value.items()), columns=['项目', '数值'])
                result_parts.append(format_dataframe(sub_df))
            except:
                # 转换失败则直接显示文本
                for sub_k, sub_v in value.items():
                    result_parts.append(f"- {sub_k}: {sub_v}")
            result_parts.append("") # 空行
            
        elif isinstance(value, list):
            result_parts.append(f"**{key}**:")
            # 检查列表是否全是字典，如果是，尝试转 DataFrame
            if value and isinstance(value[0], dict):
                 try:
                    sub_df = pd.DataFrame(value)
                    result_parts.append(format_dataframe(sub_df))
                 except:
                    for item in value:
                        result_parts.append(f"- {item}")
            else:
                for item in value:
                    result_parts.append(f"- {item}")
            result_parts.append("")
        
        elif isinstance(value, pd.DataFrame):
             result_parts.append(f"**{key}**:")
             result_parts.append(format_dataframe(value))
             result_parts.append("")
            
        else:
            # 普通键值对
            # 如果 value 也是一段很长的 HTML，也清理一下
            if isinstance(value, str) and ('<' in value and '>' in value):
                value = clean_html(value)
            # 如果 value 是很长的 DataFrame 字符串表示（用户遇到的情况），尝试跳过或特殊处理
            if isinstance(value, str) and "rows x" in value and "columns" in value:
                 # 这通常是 DataFrame 的默认 str() 输出，说明 pywencai 可能把 df 转成了 str
                 # 这里我们可能无法还原，只能建议用户看上面的表格（如果有）
                 continue 
            
            result_parts.append(f"**{key}**: {value}")
            
    return "\n".join(result_parts)

@register("wencai_plugin", "fuchaohan", "同花顺问财查询插件", "1.0.0")
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
            # 在线程池中运行 pywencai，避免阻塞主线程
            # pywencai 内部调用 Node.js 执行 JS，比较耗时
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: pywencai.get(query=query, loop=True))

            if res is None or (isinstance(res, pd.DataFrame) and res.empty):
                yield event.plain_result("未查询到相关数据。")
                return

            if isinstance(res, pd.DataFrame):
                # 数据清洗：移除一些无用的列（如 url, code 等如果不需要展示）
                # 这里只做简单的截断处理，避免消息过长
                df = res
                
                # 使用新的格式化函数
                markdown_table = format_dataframe(df)
                
                yield event.plain_result(f"查询结果：\n\n{markdown_table}")
                
            elif isinstance(res, dict):
                # 处理字典类型的返回结果（如百科类查询）
                formatted_msg = format_dict_result(res)
                yield event.plain_result(f"查询结果：\n\n{formatted_msg}")
                
            else:
                # 其他类型直接转字符串
                yield event.plain_result(f"查询结果：\n{str(res)}")

        except Exception as e:
            error_msg = str(e)
            if "node" in error_msg.lower():
                yield event.plain_result("查询失败：未检测到 Node.js 环境，请联系管理员安装 Node.js。")
            else:
                logger.error(f"问财查询出错: {e}")
                yield event.plain_result(f"查询出错: {error_msg}")
