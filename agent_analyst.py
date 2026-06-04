import os
import pandas as pd
from langchain_openai import ChatOpenAI
from langchain_experimental.agents.agent_toolkits import create_pandas_dataframe_agent

class AgentAnalyst:
    def __init__(self, api_key: str, base_url: str, model_name: str = "deepseek-chat"):
        self.llm = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            temperature=0 
        )

    # 1. 函数参数增加 global_mapping
    def analyze(self, dfs_dict: dict, current_command: str, chat_history: list, global_mapping: dict):
        """支持多表联合分析与代码执行的智能引擎"""
        df_names = list(dfs_dict.keys())
        df_list = list(dfs_dict.values())
        
        mapping_info = "\n".join([f"df{i+1} 代表: {name}" for i, name in enumerate(df_names)])
        history_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in chat_history])
        
        system_prompt = f"""你是一个高级数据科学家与精通 Pandas 的数据分析程序员。你有以下数据表变量可供在本地执行：
{mapping_info}

【核心业务计算规范】
1. 数据预处理规则：严格按照按照用户指令进行预处理
2. 索引规范：处理数据时不要用索引（请使用 reset_index(drop=True) 等方法）。
3. 基础逻辑：出租率同比差异 = 2026期数据 - 2025期数据；其他指标同比差异 = 2026期数据 / 2025期数据 - 1。

【🚨 极其重要的输出格式规范（必须遵守）】
因为你运行在严格的代理框架中，你的最终回复**必须**以 "Final Answer: " 作为开头，否则系统会崩溃！
1. 如果用户仅询问结论：请用中文直接返回结论，格式必须是 -> `Final Answer: [你的中文结论]`
2. 如果包含“生成”、“下载”、“前十”等数据提取操作：请务必在代码最后使用 `df.to_csv('temp_result.csv', index=False, encoding='utf-8-sig')` 导出文件！执行成功后，你只需要回复这几个字 -> `Final Answer: 文件已生成`。千万不要输出其他多余的解释！
3. 如果用户需要数据并分析（如：取营收前十的数据并分析）：请完整给出所取的文件并给出分析。

【历史聊天记录】
{history_str}

当前用户的最新指令是：{current_command}"""

        # ==========================================
        # 【黑魔法：指令本地加密拦截器】
        # 在发给大模型前，把提示词里的真实名字全部替换成对应的 ID！
        # ==========================================
        to_fake_dict = global_mapping.get("global", {}).get("to_fake", {})
        for real_name, fake_id in to_fake_dict.items():
            if isinstance(real_name, str): # 确保只替换文本
                system_prompt = system_prompt.replace(real_name, fake_id)
                # 连用户当前的聊天指令也一起加密了
                current_command = current_command.replace(real_name, fake_id)


        # 启动 Pandas 代码执行沙盒 (注意：这里用的是替换后的 system_prompt)
        agent = create_pandas_dataframe_agent(
            self.llm,
            df_list, 
            verbose=True, 
            allow_dangerous_code=True,
            prefix=system_prompt,
            agent_executor_kwargs={
                "handle_parsing_errors": True
            }
        )
        
        try:
            if os.path.exists("temp_result.csv"):
                os.remove("temp_result.csv")
                
            # 执行指令 (注意：这里用的是替换后的 current_command)
            response = agent.invoke(current_command)
            answer = response.get("output", "")
            
            if os.path.exists("temp_result.csv"):
                result_df = pd.read_csv("temp_result.csv")
                os.remove("temp_result.csv")
                return result_df 
            else:
                return answer 

        except Exception as e:
            return f"❌ AI 在执行本地代码时遭遇错误: {e}"