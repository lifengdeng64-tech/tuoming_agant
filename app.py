import streamlit as st
import pandas as pd
import os
import io
from dotenv import load_dotenv

from agent_hands import DesensitizationAgent
from agent_brain import LangChainAgentBrain
from agent_analyst import AgentAnalyst

st.set_page_config(page_title="全自动数据科学家 Agent", page_icon="🛡️", layout="wide")

load_dotenv()
API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
ANALYST_API_KEY = os.getenv("ANALYST_API_KEY")
ANALYST_BASE_URL = os.getenv("ANALYST_BASE_URL")
ANALYST_MODEL_NAME = os.getenv("ANALYST_MODEL_NAME", "mimo-v2.5-pro")

# ----------------- 核心：多表记忆与状态管理 -----------------
if "uploaded_file_names" not in st.session_state:
    st.session_state.uploaded_file_names = set()
if "is_masked" not in st.session_state:
    st.session_state.is_masked = False
if "safe_dfs_dict" not in st.session_state:
    st.session_state.safe_dfs_dict = {}  
if "global_mapping" not in st.session_state:
    st.session_state.global_mapping = {} 
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

def unmask_report(report_text, mapping_dict):
    """本地解敏还原函数（针对纯文本）"""
    if not isinstance(report_text, str):
        return report_text
    unmasked_text = report_text
    for col, data in mapping_dict.items():
        for fake_id, real_name in data["to_fake"].items():
            unmasked_text = unmasked_text.replace(str(data["to_fake"][fake_id]), str(fake_id))
    return unmasked_text

# ----------------- UI 与 逻辑主流程 -----------------
st.title("🛡️ 全自动数据科学家 Agent (终极版)")
st.markdown("支持多表联合分析、自动编写代码修改数据，并直接生成新的 Excel 文件供下载！")

if not API_KEY or not ANALYST_API_KEY:
    st.error("❌ 找不到 API Key！请检查 .env 文件。")
    st.stop()

# 【改造点 1：支持多文件上传】
uploaded_files = st.file_uploader("📂 请上传业务报表 (支持多选 Excel/CSV)", type=["xlsx", "csv"], accept_multiple_files=True)

if uploaded_files:
    current_file_names = {file.name for file in uploaded_files}
    
    if st.session_state.uploaded_file_names != current_file_names:
        st.session_state.uploaded_file_names = current_file_names
        st.session_state.is_masked = False
        st.session_state.safe_dfs_dict = {}
        st.session_state.global_mapping = {}
        st.session_state.chat_history = []
        st.warning("🔄 检测到上传文件发生变化，所有历史记忆和脱敏字典已彻底清空！")

    try:
        raw_dfs_dict = {}
        for file in uploaded_files:
            if file.name.endswith('.csv'):
                raw_dfs_dict[file.name] = pd.read_csv(file)
            else:
                raw_dfs_dict[file.name] = pd.read_excel(file)
            
        # ==========================================
        # 阶段一：尚未脱敏时，显示【全局脱敏控制台】
        # ==========================================
        if not st.session_state.is_masked:
            st.info(f"成功加载 {len(raw_dfs_dict)} 份数据。请先设置全局安全边界。")
            
            headers_dict = {name: df.columns.tolist() for name, df in raw_dfs_dict.items()}
            with st.expander("👀 查看各文件表头"):
                st.json(headers_dict)
                
            mask_command = st.text_input("🗣️ 请输入全局脱敏指令（如：把所有表里的门店名称和第一列脱敏）：")
            
            if st.button("🔒 确认执行多表脱敏并锁定", type="primary"):
                if not mask_command:
                    st.warning("⚠️ 请先输入脱敏指令！")
                else:
                    with st.spinner("🤖 大脑正在并发解析多表表头..."):
                        brain = LangChainAgentBrain(api_key=API_KEY, base_url=BASE_URL)
                        hands = DesensitizationAgent()
                        
                        target_columns_map = brain.decide_columns(mask_command, headers_dict)
                        
                        if not any(target_columns_map.values()):
                            st.error("⚠️ 大脑未识别出任何需要脱敏的列，请重新输入。")
                        else:
                            st.write(f"✅ 大脑决策完毕，目标列：{target_columns_map}")
                            with st.spinner("⚙️ 正在进行跨表联合哈希加密..."):
                                # 【修复点：这里调用了最新的 global_mask 方法】
                                safe_dfs_dict, global_mapping = hands.global_mask(raw_dfs_dict, target_columns_map)
                                
                                st.session_state.safe_dfs_dict = safe_dfs_dict
                                st.session_state.global_mapping = global_mapping
                                st.session_state.is_masked = True
                                st.rerun()

        # ==========================================
        # 阶段二：脱敏完成后，呈现【代码执行与对话控制台】
        # ==========================================
        else:
            st.success("✅ 多表数据已完成全局锁定！跨表主键已对齐。")
            with st.expander("👀 查看底层已脱敏的安全数据预览"):
                for name, df in st.session_state.safe_dfs_dict.items():
                    st.write(f"**{name}**")
                    st.dataframe(df.head(3))
            
            st.divider()

            for msg in st.session_state.chat_history:
                with st.chat_message(msg["role"]):
                    if isinstance(msg["content"], pd.DataFrame):
                        st.dataframe(msg["content"])
                    else:
                        st.markdown(msg["content"])
            
            if analysis_command := st.chat_input("🗣️ 输入指令 (如: 将两张表合并，提取前10条数据并生成文件)"):
                with st.chat_message("user"):
                    st.markdown(analysis_command)
                
                with st.chat_message("assistant"):
                    with st.spinner("🧠 AI 正在编写并执行 Pandas 代码..."):
                        analyst = AgentAnalyst(
                            api_key=ANALYST_API_KEY, 
                            base_url=ANALYST_BASE_URL,
                            model_name=ANALYST_MODEL_NAME
                        )
                        
                        raw_result = analyst.analyze(
                            st.session_state.safe_dfs_dict, 
                            analysis_command,
                            st.session_state.chat_history,
                            st.session_state.global_mapping
                        )
                        
                        hands = DesensitizationAgent() 
                        
                        if isinstance(raw_result, pd.DataFrame):
                            final_df = hands.unmask_dataframe(raw_result, st.session_state.global_mapping)
                            st.dataframe(final_df)
                            
                            buffer = io.BytesIO()
                            with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
                                final_df.to_excel(writer, index=False, sheet_name='AI_Result')
                            st.download_button(
                                label="📥 下载处理后的 Excel 文件",
                                data=buffer.getvalue(),
                                file_name="AI处理结果.xlsx",
                                mime="application/vnd.ms-excel"
                            )
                            st.session_state.chat_history.append({"role": "user", "content": analysis_command})
                            st.session_state.chat_history.append({"role": "assistant", "content": final_df, "raw_content": raw_result})
                            
                        else:
                            final_text = unmask_report(raw_result, st.session_state.global_mapping)
                            st.markdown(final_text)
                            
                            st.session_state.chat_history.append({"role": "user", "content": analysis_command})
                            st.session_state.chat_history.append({"role": "assistant", "content": final_text, "raw_content": raw_result})

    except Exception as e:
        st.error(f"❌ 系统发生错误: {e}")