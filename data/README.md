# data/

运行期数据目录。**内容不进版本库**（见根目录 `.gitignore`）。

| 文件 | 说明 |
|---|---|
| `master.key` | 加密主密钥（首次启动自动生成）⚠️ **务必备份** |
| `hero_sms.key` | hero-sms 接码密钥 |
| `fivesim.key` | 5sim 接码密钥 |
| `sms_provider.txt` | 当前接码商（hero / 5sim） |
| `app.db` | SQLite：账号池、用量、下游 Key、日志 |
| `profiles/` | 每账号一个浏览器指纹档案 |
| `workspace/` | 本地工作区（agent 读写沙箱） |

密钥也可以在网页「注册 → 接码配置」里填写，会自动写到这个目录。
