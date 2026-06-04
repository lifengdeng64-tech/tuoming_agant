import pandas as pd
import hashlib

class DesensitizationAgent:
    def __init__(self):
        # 终极版：我们采用“全局名册”机制，而不是每张表独立建字典
        self.global_to_fake = {}
        self.global_to_real = {}

    def _generate_fake_id(self, real_value: str) -> str:
        """核心哈希算法，保证相同文字永远生成相同的 ID"""
        hash_obj = hashlib.md5(str(real_value).encode('utf-8')).hexdigest()
        return f"ID_{hash_obj[:8].upper()}"

    def global_mask(self, raw_dfs_dict: dict, target_columns_map: dict):
        """执行多表联合脱敏"""
        safe_dfs_dict = {}
        
        # 遍历字典里的每一张表
        for df_name, df in raw_dfs_dict.items():
            safe_df = df.copy()
            # 获取大脑指定的、当前表需要脱敏的列
            cols_to_mask = target_columns_map.get(df_name, [])
            
            for col in cols_to_mask:
                if col in safe_df.columns:
                    unique_vals = safe_df[col].dropna().unique()
                    for val in unique_vals:
                        if val not in self.global_to_fake:
                            fake_id = self._generate_fake_id(val)
                            self.global_to_fake[val] = fake_id
                            self.global_to_real[fake_id] = val
                    
                    # 用全局字典替换真实数据
                    safe_df[col] = safe_df[col].map(lambda x: self.global_to_fake.get(x, x))
            
            safe_dfs_dict[df_name] = safe_df
        
        # 为了兼容 app.py 的反向解析格式，将全局字典包装成 {"global": {...}}
        global_mapping = {
            "global": {
                "to_fake": self.global_to_fake,
                "to_real": self.global_to_real
            }
        }
        
        return safe_dfs_dict, global_mapping

    def unmask_dataframe(self, df: pd.DataFrame, global_mapping: dict) -> pd.DataFrame:
        """核心还原术：大模型生成新表后，一键将所有 ID 洗回真实中文"""
        unmasked_df = df.copy()
        to_real_dict = global_mapping.get("global", {}).get("to_real", {})
        
        if to_real_dict:
            # Pandas 的 replace 支持直接传入全局字典进行全表盲搜替换，性能极高
            unmasked_df = unmasked_df.replace(to_real_dict)
            
        return unmasked_df