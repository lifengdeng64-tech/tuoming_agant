from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from typing import Dict, List

# 1. 升级数据结构规范：从单列表变成“字典（表名 -> 列名列表）”
class GlobalMaskingDecision(BaseModel):
    target_columns_map: Dict[str, List[str]] = Field(
        description="字典格式。Key 是对应的表格名称，Value 是该表需要脱敏的【真实表头名列表】。如果某张表不需要脱敏，Value请设为空列表 []。"
    )

class LangChainAgentBrain:
    def __init__(self, api_key: str, base_url: str, model_name: str = "deepseek-chat"):
        self.llm = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            temperature=0
        )
        
        self.parser = PydanticOutputParser(pydantic_object=GlobalMaskingDecision)
        
        # 2. 升级提示词：全面适应多表结构 + 位置翻译能力
        self.prompt_template = ChatPromptTemplate.from_messages([
            ("system", """你是一个专业的数据安全与业务分析网关。
用户的指令可能涉及多张数据表，你的任务是从下方提供的【多张表格的表头字典】中，分别找出每张表需要脱敏处理的实体列。

【核心推导规则】
1. 实体识别：用户指令中提到的需要脱敏的实体（如人名、店名、大区、ID等），请在各表中寻找对应的表头。
2. 位置翻译（极其重要）：如果用户使用了“第一列”、“前两列”等基于位置的代词，请你自动去下方对应表格的表头列表中（从第1个开始）按顺序去数，找出真实的表头名称！如果用户没有指定是哪张表，默认将这个位置规则应用到所有上传的表中。
3. 业务红线：对于类似 RevPAR、出租率、GOP率、完成度、营业收入等核心业务计算指标，绝对不能进行脱敏。
4. 严格匹配：你最终输出的列名，必须严格存在于对应表格的真实表头中！绝不能直接输出“第一列”这种词汇。

【输出格式严格要求】
{format_instructions}

【当前所有表格及其表头】
{headers_dict}"""),
            ("user", "我的指令是：{user_command}")
        ])
        
        self.chain = self.prompt_template | self.llm | self.parser

    # 3. 升级输入输出逻辑
    def decide_columns(self, user_command: str, headers_dict: dict) -> dict:
        """执行推理，返回每张表需要脱敏的列名映射字典"""
        print("🧠 大脑正在并发解析多表脱敏指令...")
        try:
            result = self.chain.invoke({
                "headers_dict": headers_dict,
                "user_command": user_command,
                "format_instructions": self.parser.get_format_instructions()
            })
            return result.target_columns_map
        except Exception as e:
            print(f"❌ LangChain 多表推理出错: {e}")
            return {}