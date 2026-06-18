# 例外匹配列表
# 格式：每行 NDF通道名 -> 匹配通道名 （支援 -> : | = ，載入自動 trim）
# 匹配時不區分大小寫；保存後會刷新目前事件顯示（若已載入）。

apnea -> flow
apnea -> thermistor
b-snoring -> snore
desaturation -> spo2
hypopnea -> thermistor
hypopnea -> flow
hypopnea -> nasal pressure
single snore -> snore
snore -> snore-CU
snore -> snoredB
snore train -> snore
snore-CU -> snore
snoredB -> snore
