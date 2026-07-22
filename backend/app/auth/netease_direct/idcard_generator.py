"""中国身份证号码生成器。

逆向来源: CYXHSJ 永久Cookies获取.exe (PyInstaller Python 3.13)
生成符合 GB 11643-1999 标准的 18 位身份证号码,用于 4399 注册。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

# 常见地区代码 (前6位)
AREA_CODES = [
    "110108",  # 北京海淀区
    "310104",  # 上海徐汇区
    "440106",  # 广州天河区
    "440304",  # 深圳福田区
    "330106",  # 杭州西湖区
    "320105",  # 南京建邺区
    "370102",  # 济南历下区
    "420102",  # 武汉江岸区
    "500103",  # 重庆渝中区
    "610102",  # 西安新城区
]

# 姓氏列表
SURNAMES = (
    "李王张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓"
    "许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹"
    "熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段漕钱汤尹黎易常武乔贺赖龚文"
)

# 名字用字
NAME_CHARS = (
    "伟芳娜秀英敏静丽强磊洋艳勇军杰娟涛明超秀峰霞平刚桂英华慧巧美娜静淑"
    "惠珠翠雅芝玉萍红娥玲芬芳燕彩春菊兰凤洁梅琳素云莲真环雪荣爱妹霞香月"
    "莺媛艳瑞凡佳嘉琼斌能树烈豪发可然飞翔宇轩浩然俊杰博思睿明轩梓宸皓轩"
)

# 身份证校验码权重
_IDCARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_IDCARD_CHECK_CODES = ["1", "0", "X", "9", "8", "7", "6", "5", "4", "3", "2"]


def generate_idcard(
    *,
    area_code: str | None = None,
    birth_start: datetime | None = None,
    birth_end: datetime | None = None,
) -> str:
    """生成符合标准的 18 位中国身份证号码。

    Args:
        area_code: 6 位地区码, None 则随机
        birth_start: 生日范围起始, 默认 1970-01-01
        birth_end: 生日范围结束, 默认 2004-12-31

    Returns:
        18 位身份证号码字符串
    """
    if area_code is None:
        area_code = random.choice(AREA_CODES)
    if birth_start is None:
        birth_start = datetime(1970, 1, 1)
    if birth_end is None:
        birth_end = datetime(2004, 12, 31)

    # 随机生日
    delta = birth_end - birth_start
    random_days = random.randint(0, delta.days)
    birth_date = birth_start + timedelta(days=random_days)
    birthday_str = birth_date.strftime("%Y%m%d")

    # 随机 3 位序号
    seq = random.randint(100, 999)

    # 前 17 位
    idcard_17 = area_code + birthday_str + str(seq)

    # 计算校验码
    total = sum(int(idcard_17[i]) * _IDCARD_WEIGHTS[i] for i in range(17))
    check_code = _IDCARD_CHECK_CODES[total % 11]

    return idcard_17 + check_code


def generate_name() -> str:
    """生成随机中文姓名。"""
    surname = random.choice(SURNAMES)
    name_len = random.choice([1, 2])  # 单字名或双字名
    given_name = "".join(random.choice(NAME_CHARS) for _ in range(name_len))
    return surname + given_name


def generate_username(length: int = 8) -> str:
    """生成随机数字用户名。"""
    return "".join(random.choice("0123456789") for _ in range(length))


def generate_password(length: int = 12) -> str:
    """生成随机密码 (字母+数字)。"""
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(chars) for _ in range(length))
