"""Causal five-state crude oil regime router (zero look-ahead, stateless replay).

Classifies global Brent crude oil daily structure strictly from closed history
(`< trade_date`) into five regimes (`上升`, `震荡上行`, `震荡横盘`, `中继下跌`,
`下降`), applies dual-speed EWMA smoothing and asymmetric hysteresis locking,
computes crude-cluster dual-key MA confirmation (`g_crude`), and optionally
overlays a read-only intraday Fast Crash ramp without mutating daily cache.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import json
import math
from statistics import median
from threading import RLock
from typing import Any, Mapping, Sequence

from .v9_plus_engine import V9MacroDataStore

OIL_REGIMES: tuple[str, ...] = ("上升", "震荡上行", "震荡横盘", "中继下跌", "下降")

CRUDE_CLUSTER_SYMBOLS: frozenset[str] = frozenset({"160723", "161129", "501018"})

V62_MACRO_CACHE_KEYS: tuple[str, ...] = (
    "oil_daily_2y",
    "ho_daily_2y",
    "cl_daily_2y",
    "ng_daily_2y",
    "uso_daily_2y",
    "bno_daily_2y",
    "dbo_daily_2y",
    "sc0_daily_all",
    "xop_daily_2y",
    "xle_daily_2y",
    "spy_daily_2y",
    "iwm_daily_2y",
)

V62_DIMENSION_ORDER: tuple[str, ...] = (
    "cta_trend",
    "meanrev_vol",
    "crack_demand",
    "term_physical",
    "smart_equity",
)

V62_DIMENSION_SHORT_LABELS: dict[str, str] = {
    "cta_trend": "趋势动量",
    "meanrev_vol": "超跌波动",
    "crack_demand": "炼化裂解",
    "term_physical": "期限实货",
    "smart_equity": "油气聪明钱",
}

FROZEN_V62_PRIOR_WEIGHTS: dict[str, float] = {
    "cta_trend": 0.259,
    "crack_demand": 0.1995,
    "term_physical": 0.1882,
    "smart_equity": 0.0908,
    "meanrev_vol": 0.2624,
}

FROZEN_V62_CONFORMAL_PAD: tuple[float, ...] = (0.276, 0.2531, 0.1888, 0.3126, 0.1948)

FROZEN_V62_OOF_CONS_QUANTILES: dict[str, float] = {
    "q33": 51.9,
    "q50": 68.0,
    "q67": 74.5,
}

# Frozen V6.2 5-Dimension Structural Committee parameters (strictly fitted on 20210901~20231214, label end <= 20231229)
FROZEN_V62_DIM_SPECS: dict[str, dict[str, Any]] = {
    "cta_trend": {
        "cont_keys": (
            "px_ma15",
            "px_ma20",
            "px_ma60",
            "ma5_ma15",
            "ma15_ma60",
            "ma20_slope5",
            "ret5",
            "ret10",
            "ret20",
            "er20",
        ),
        "ind_keys": ("ind_bull", "ind_bear"),
        "means": {
            "px_ma15": 0.035542197124396165,
            "px_ma20": 0.10570146286169667,
            "px_ma60": 0.6359569259503677,
            "ma5_ma15": 0.04604934513546363,
            "ma15_ma60": 0.5811807117977449,
            "ma20_slope5": 0.10367143461926487,
            "ret5": 0.10843472798256289,
            "ret10": 0.2569773432485108,
            "ret20": 0.7117602771573445,
            "er20": 0.2204794933377258,
        },
        "stds": {
            "px_ma15": 4.00740914427089,
            "px_ma20": 4.606133750485345,
            "px_ma60": 8.016535098160025,
            "ma5_ma15": 2.825439077241114,
            "ma15_ma60": 6.223891392996986,
            "ma20_slope5": 2.0036683675268647,
            "ret5": 4.647244713223508,
            "ret10": 6.246541586063913,
            "ret20": 8.575119443874938,
            "er20": 0.16850627735877338,
        },
        "clips": {
            "px_ma15": (-9.18119326535406, 7.661244594911895),
            "px_ma20": (-10.145411315900288, 8.711524719680451),
            "px_ma60": (-13.398962498255809, 19.7853231187493),
            "ma5_ma15": (-5.8116746843089615, 5.90158169577373),
            "ma15_ma60": (-9.040514875998891, 16.370539983417274),
            "ma20_slope5": (-3.698641691052443, 4.0973029386248685),
            "ret5": (-10.343035196913734, 10.378259466984254),
            "ret10": (-13.034550038757624, 13.897569533692192),
            "ret20": (-15.735331858468516, 19.455023440913333),
            "er20": (0.005538724022382103, 0.6220957837791471),
        },
        "weights": (
            (
                -0.6481556352516825,
                0.21882314975850098,
                0.26269452755263545,
                0.14726610897731807,
                0.292097660620831,
                -0.02290330368576104,
                0.2885065447108699,
                -0.06101750007192011,
                0.12944947131029458,
                0.15647768793851846,
                0.36615725758832945,
                0.3469486841112256,
                -0.10945140029614406,
            ),
            (
                0.2518445559669705,
                0.019187659417983213,
                0.07083600884418563,
                0.22618634599580695,
                0.39446364988235244,
                0.3301776419753078,
                0.05519643862646715,
                -0.09468834458016408,
                0.16535640046427932,
                -0.06209490232649218,
                -0.15533763412871546,
                -0.12637998011423812,
                0.08362294340570327,
            ),
            (
                0.31839683749550163,
                -0.05814935261154392,
                -0.03573147151566461,
                -0.10183209529049703,
                0.09093167799566212,
                -0.09036521320703184,
                -0.0777779111221864,
                0.011729676129186828,
                0.040400112708074774,
                0.06712094632791542,
                -0.10568391460686792,
                -0.07074969675818334,
                -0.13214296513321397,
            ),
            (
                0.3223711537207689,
                -0.126101023486195,
                -0.13277072955541888,
                -0.16264987890281246,
                -0.34208949692722435,
                -0.13995536204197198,
                -0.166049301703866,
                0.12362139519190844,
                -0.2049474098856149,
                -0.13943476854091816,
                -0.004071281047722486,
                -0.008821708908361144,
                0.2864826956680234,
            ),
            (
                -0.24445691582214454,
                -0.05376043307874456,
                -0.1650283353257381,
                -0.10897048077981633,
                -0.4354034915716217,
                -0.07695376304054302,
                -0.09987577051128525,
                0.0203547733309892,
                -0.13025857459703283,
                -0.022068963399022524,
                -0.10106442780502371,
                -0.14099729833044244,
                -0.1285112736443688,
            ),
        ),
        "temperature": 0.72,
    },
    "crack_demand": {
        "cont_keys": (
            "crack_ret3",
            "crack_ret10",
            "crack_ma_gap",
            "ho_pos25",
            "ho_ma5_ma15",
            "wti_brn_ret5",
            "ng_ma5_ma15",
        ),
        "ind_keys": (),
        "means": {
            "crack_ret3": 0.14832944614479418,
            "crack_ret10": 0.3741456678561696,
            "crack_ma_gap": 0.13579336088381294,
            "ho_pos25": 50.202458026140974,
            "ho_ma5_ma15": 0.06607782715505664,
            "wti_brn_ret5": 0.004245337242790977,
            "ng_ma5_ma15": -0.5559280526072989,
        },
        "stds": {
            "crack_ret3": 2.9425761068212752,
            "crack_ret10": 4.66492788774197,
            "crack_ma_gap": 2.146331871260718,
            "ho_pos25": 34.42698894755781,
            "ho_ma5_ma15": 3.6083332902132006,
            "wti_brn_ret5": 1.0680920242566285,
            "ng_ma5_ma15": 6.458134189050984,
        },
        "clips": {
            "crack_ret3": (-7.142955884310185, 6.9865983694586395),
            "crack_ret10": (-10.766921401935793, 12.268450131979796),
            "crack_ma_gap": (-5.116871760619433, 5.371070640426812),
            "ho_pos25": (0.0, 100.0),
            "ho_ma5_ma15": (-7.314545512266369, 7.571192619069242),
            "wti_brn_ret5": (-2.690801982925205, 2.523358191458305),
            "ng_ma5_ma15": (-14.801835576496236, 12.712230663089889),
        },
        "weights": (
            (
                -0.737674638334649,
                0.0509490651933716,
                -0.41212851817785984,
                -0.6119649618949083,
                1.1270287100079273,
                0.7421963588836904,
                0.18253002582795946,
                0.35217783930054253,
            ),
            (
                0.27718899425136306,
                -0.11694811345119045,
                -0.12032999542918117,
                -0.20031223180150123,
                0.18880327714147863,
                0.48167268530194324,
                0.042583531034181776,
                0.030343234104159254,
            ),
            (
                0.24348481467905525,
                0.041114630218901035,
                0.14307931202907925,
                -0.09955761715116242,
                -0.11245620960755551,
                0.11801552216977841,
                -0.1035641043141187,
                -0.4391326724472807,
            ),
            (
                0.5701201951675461,
                0.23206440679355722,
                0.05155050599775146,
                0.41253707756935454,
                -0.4066033061237994,
                -0.8389352952712528,
                -0.11671043590771307,
                -0.04835515435827431,
            ),
            (
                -0.35311936743860756,
                -0.20717998875463903,
                0.3378286955802127,
                0.4992977332782172,
                -0.7967724714180503,
                -0.5029492710841593,
                -0.004839016640309626,
                0.10496675340085487,
            ),
        ),
        "temperature": 0.72,
    },
    "term_physical": {
        "cont_keys": (
            "roll_uso_dbo_5",
            "roll_uso_dbo_20",
            "roll_bno_dbo_10",
            "sc_brn_ret5",
            "sc_brn_ma_diff",
            "sc0_pos25",
            "cl_brn_ratio_z20",
        ),
        "ind_keys": (),
        "means": {
            "roll_uso_dbo_5": 0.16517632110020922,
            "roll_uso_dbo_20": 0.6343899076406495,
            "roll_bno_dbo_10": 0.3769234096675401,
            "sc_brn_ret5": 0.15447007499111173,
            "sc_brn_ma_diff": 0.18325539711433766,
            "sc0_pos25": 55.88048299435355,
            "cl_brn_ratio_z20": -0.037308387952093944,
        },
        "stds": {
            "roll_uso_dbo_5": 0.8763061343861268,
            "roll_uso_dbo_20": 1.9934508357082947,
            "roll_bno_dbo_10": 1.5903763158377187,
            "sc_brn_ret5": 3.6544452490041626,
            "sc_brn_ma_diff": 1.4931147608364566,
            "sc0_pos25": 34.91695727638926,
            "cl_brn_ratio_z20": 1.2678680532083542,
        },
        "clips": {
            "roll_uso_dbo_5": (-2.041234421358073, 2.7827435718627798),
            "roll_uso_dbo_20": (-3.7667586541467575, 7.373854184328319),
            "roll_bno_dbo_10": (-3.8015645121638575, 5.241765541933712),
            "sc_brn_ret5": (-9.204637348052284, 7.927807944420861),
            "sc_brn_ma_diff": (-3.1388701734604907, 3.279631357650163),
            "sc0_pos25": (0.0, 100.0),
            "cl_brn_ratio_z20": (-2.5069843521940633, 2.2997673890294967),
        },
        "weights": (
            (
                -0.4338655785870901,
                -0.19495102828788974,
                0.16813937976894763,
                0.269785686326081,
                0.03650002658161235,
                -0.017932709019693654,
                0.9532023444434693,
                0.4404835980266839,
            ),
            (
                0.19309598503425,
                0.03350901923147508,
                0.05240502082663034,
                0.05589766902059069,
                -0.005068956673671569,
                0.14476171359889223,
                0.35978536657144367,
                0.2339540134989986,
            ),
            (
                0.127400003273141,
                -0.11228270432841272,
                -0.10043649093416865,
                -0.09562993084625072,
                0.05446973505778876,
                -0.15093922777627714,
                -0.019830781144545725,
                -0.2623971287023661,
            ),
            (
                0.43864042095174155,
                0.0487559114871435,
                -0.10036414761523989,
                -0.12687722463295217,
                -0.09861192144939201,
                0.12067655233034742,
                -0.7198004836787119,
                -0.2797597629006532,
            ),
            (
                -0.3252708332365941,
                0.22496880189768426,
                -0.019743762046169618,
                -0.1031761998674691,
                0.012711116483661727,
                -0.0965663291332697,
                -0.5733564461916562,
                -0.13228071992266358,
            ),
        ),
        "temperature": 0.72,
    },
    "smart_equity": {
        "cont_keys": (
            "xop_rs5",
            "xop_rs20",
            "xle_rs10",
            "xop_vs_oil5",
            "xop_ma5_ma15",
            "xop_pos25",
            "macro_risk_on",
        ),
        "ind_keys": (),
        "means": {
            "xop_rs5": 0.5194889614569839,
            "xop_rs20": 2.217814402025552,
            "xle_rs10": 1.0152348919985137,
            "xop_vs_oil5": 0.40577442354551907,
            "xop_ma5_ma15": 0.5200964283903646,
            "xop_pos25": 59.336148291330794,
            "macro_risk_on": -0.27549963963691315,
        },
        "stds": {
            "xop_rs5": 4.538152967003508,
            "xop_rs20": 9.798650449618851,
            "xle_rs10": 5.627987144428341,
            "xop_vs_oil5": 3.84751997976606,
            "xop_ma5_ma15": 3.3597605195646416,
            "xop_pos25": 35.37991161782517,
            "macro_risk_on": 1.9821157890050871,
        },
        "clips": {
            "xop_rs5": (-9.169630697449016, 11.121184131941595),
            "xop_rs20": (-17.324196302303918, 23.27653306054394),
            "xle_rs10": (-10.081094220512748, 15.593612043598085),
            "xop_vs_oil5": (-8.458214051564203, 9.842376303954705),
            "xop_ma5_ma15": (-7.582469145106874, 7.30623731835961),
            "xop_pos25": (0.0, 100.0),
            "macro_risk_on": (-5.06170480787846, 3.5836180837024703),
        },
        "weights": (
            (
                -0.33844405130094474,
                0.06806154736629286,
                0.4021749394906016,
                0.3846066791839466,
                -0.14703568091067726,
                0.06626534271600566,
                0.39118765672738176,
                0.08513149303412049,
            ),
            (
                0.212410964121453,
                -0.0394402267093914,
                0.05329235619619254,
                0.29066495579028084,
                -0.11474462178646058,
                0.26076145600896716,
                -0.032817036916381846,
                -0.15317815304910917,
            ),
            (
                0.11134281464242109,
                0.060158185260569555,
                -0.02247467296710641,
                -0.1789757318384019,
                -0.11530181474035338,
                0.11566665332348476,
                -0.037004558078578914,
                0.015979916837517216,
            ),
            (
                0.4366915874756584,
                0.07440177277587166,
                -0.25154246005368186,
                -0.2931550131321993,
                0.2982783122765127,
                -0.2575561275378945,
                -0.18486254196293758,
                0.12444356822020397,
            ),
            (
                -0.4220013184804174,
                -0.163181278693343,
                -0.18145016266600575,
                -0.20314089000362565,
                0.0788038051609779,
                -0.18513732451056303,
                -0.13650351976948427,
                -0.07237682504273246,
            ),
        ),
        "temperature": 0.72,
    },
    "meanrev_vol": {
        "cont_keys": (
            "dd_hi25",
            "plunge25",
            "reb_lo10",
            "pos25",
            "vol20",
            "vol5_ratio",
            "ret3",
        ),
        "ind_keys": ("ind_relay", "ind_oversold_exhaust", "ind_overbought_exhaust"),
        "means": {
            "dd_hi25": -6.438613690222013,
            "plunge25": -10.513936947492267,
            "reb_lo10": 4.448014470212173,
            "pos25": 53.95947539722285,
            "vol20": 2.216849883131933,
            "vol5_ratio": 0.8797584214001173,
            "ret3": 0.06781262013625967,
        },
        "stds": {
            "dd_hi25": 5.601334314938697,
            "plunge25": 5.24622302993775,
            "reb_lo10": 3.681876131317817,
            "pos25": 33.93363570188109,
            "vol20": 0.8677830165101245,
            "vol5_ratio": 0.2815383488776812,
            "ret3": 3.849874461255972,
        },
        "clips": {
            "dd_hi25": (-19.646580872768574, 0.0),
            "plunge25": (-24.252980337513552, -2.7722772277227636),
            "reb_lo10": (0.0, 15.050165703055622),
            "pos25": (0.0, 100.0),
            "vol20": (1.0464027364671569, 5.254130586248072),
            "vol5_ratio": (0.405769954576028, 1.5289158533118945),
            "ret3": (-9.277824336906756, 8.568708271691074),
        },
        "weights": (
            (
                -0.9604841442467977,
                0.43039242990771426,
                0.2909866405673097,
                0.3969348084122719,
                1.027327875161446,
                -0.5499465495032871,
                0.10171711789523133,
                -0.3220263760814339,
                -0.10020121540570336,
                -0.0013607270120844198,
                -0.2169664230762965,
            ),
            (
                0.4725114857328237,
                0.19875798086051913,
                -0.03562320462637877,
                -0.09673285833170384,
                0.4952337876637682,
                0.48341553038148216,
                0.05248550868809717,
                -0.2508063724016147,
                -0.30637456107542355,
                -0.07336016940244065,
                0.06409113413977668,
            ),
            (
                0.3681048758925033,
                -0.1673079991716775,
                -0.09258612187484162,
                -0.031366687266337574,
                0.014643917449487148,
                0.3133596857239967,
                -0.36684619285322084,
                0.010608275220091,
                -0.4625367087418657,
                -0.0742904450679652,
                0.16207922524535354,
            ),
            (
                0.44019445703324156,
                -0.26715667891129385,
                -0.13447938224209863,
                -0.07133015640806964,
                -0.6614061357420691,
                0.17753612356853438,
                0.17326149673134286,
                0.11966429834491456,
                0.5601885055820326,
                0.2351238244295697,
                0.035296386158682094,
            ),
            (
                -0.3203266774128199,
                -0.19468573268526315,
                -0.02829793182399214,
                -0.19750510640616115,
                -0.8757994445326359,
                -0.4243647901707291,
                0.03938206953854972,
                0.44256017491804095,
                0.30892397964096086,
                -0.08611248294707943,
                -0.04450032246751592,
            ),
        ),
        "temperature": 0.72,
    },
}

FROZEN_TRAINED_WINDOW: str = "20161128~20211231"

FROZEN_FEATURE_KEYS: tuple[str, ...] = (
    "px_ma15",
    "px_ma20",
    "px_ma60",
    "ma5_ma15",
    "ma15_ma60",
    "ma20_slope5",
    "ret3",
    "ret5",
    "ret10",
    "ret20",
    "dd_hi25",
    "plunge25",
    "reb_lo10",
    "pos25",
    "er20",
    "vol20",
)

FROZEN_FEATURE_MEANS: dict[str, float] = {
    "px_ma15": 0.248487,
    "px_ma20": 0.33676,
    "px_ma60": 1.163384,
    "ma5_ma15": 0.145412,
    "ma15_ma60": 0.762798,
    "ma20_slope5": 0.155289,
    "ret3": 0.177766,
    "ret5": 0.292362,
    "ret10": 0.57909,
    "ret20": 1.146027,
    "dd_hi25": -5.670387,
    "plunge25": -9.443973,
    "reb_lo10": 4.367188,
    "pos25": 60.221868,
    "er20": 0.255497,
    "vol20": 2.012057,
}

FROZEN_FEATURE_STDS: dict[str, float] = {
    "px_ma15": 5.310354,
    "px_ma20": 6.222123,
    "px_ma60": 11.532226,
    "ma5_ma15": 3.805571,
    "ma15_ma60": 9.106566,
    "ma20_slope5": 2.756437,
    "ret3": 4.327262,
    "ret5": 5.472465,
    "ret10": 7.788105,
    "ret20": 11.14975,
    "dd_hi25": 7.912679,
    "plunge25": 8.225966,
    "reb_lo10": 5.023402,
    "pos25": 35.289452,
    "er20": 0.170601,
    "vol20": 1.483037,
}

# Frozen L2-regularized Softmax weights fitted on 20161128~20211231 (5 x 21: [1.0] + 16 z-features + 4 interactions)
FROZEN_SOFTMAX_WEIGHTS: tuple[tuple[float, ...], ...] = (
    (
        -0.339425,
        0.214596,
        0.32981,
        0.079058,
        0.25002,
        -0.034092,
        0.199411,
        0.083375,
        0.068387,
        0.171766,
        0.176699,
        0.09316,
        0.047377,
        0.044798,
        0.343294,
        0.221642,
        0.099336,
        -0.094373,
        0.057121,
        -0.060959,
        -0.025163,
    ),
    (
        0.191552,
        0.099072,
        0.076156,
        0.017396,
        0.244818,
        -0.007082,
        0.052341,
        -0.096151,
        -0.007093,
        0.147505,
        0.036467,
        0.147065,
        0.219439,
        -0.121137,
        0.349613,
        -0.105882,
        -0.011255,
        -0.116813,
        0.087454,
        0.013486,
        0.142854,
    ),
    (
        0.665815,
        -0.164603,
        -0.243742,
        -0.074428,
        -0.189665,
        -0.00811,
        -0.103161,
        -0.002092,
        -0.103752,
        -0.137722,
        -0.170681,
        0.02497,
        0.100515,
        -0.059985,
        0.126031,
        -0.183965,
        -0.09363,
        -0.238747,
        0.049161,
        0.060261,
        0.21896,
    ),
    (
        0.027188,
        0.008899,
        -0.001265,
        0.095825,
        -0.19711,
        0.072289,
        -0.12487,
        0.1357,
        0.084916,
        0.015867,
        0.016446,
        -0.175981,
        -0.416699,
        0.389742,
        -0.22108,
        -0.065998,
        0.178734,
        0.590924,
        0.003397,
        -0.110023,
        -0.134352,
    ),
    (
        -0.545129,
        -0.157964,
        -0.160958,
        -0.117851,
        -0.108064,
        -0.023004,
        -0.023721,
        -0.120832,
        -0.042458,
        -0.197416,
        -0.05893,
        -0.089214,
        0.049369,
        -0.253419,
        -0.597859,
        0.134204,
        -0.173185,
        -0.140991,
        -0.197132,
        0.097235,
        -0.202299,
    ),
)

V5_REGIME_TABLE: dict[str, dict[str, Any]] = {
    "上升": {
        "cluster_bull": 0.94,
        "cluster_unconf": 0.62,
        "sig_bull": 2.60,
        "sig_unconf": 2.30,
        "solo_c_coef": 0.38,
        "solo_c_cap": 0.52,
        "rev_t_501018": 0.52,
        "unconf_wt": 0.65,
        "taper_501018": 0.65,
        "mults": {"160723": 1.12, "161129": 0.82, "501018": 0.92, "162411": 1.22},
    },
    "震荡上行": {
        "cluster_bull": 0.96,
        "cluster_unconf": 0.66,
        "sig_bull": 2.75,
        "sig_unconf": 2.45,
        "solo_c_coef": 0.42,
        "solo_c_cap": 0.58,
        "rev_t_501018": 0.50,
        "unconf_wt": 0.68,
        "taper_501018": 0.65,
        "mults": {"160723": 1.14, "161129": 0.86, "501018": 0.96, "162411": 1.25},
    },
    "震荡横盘": {
        "cluster_bull": 0.90,
        "cluster_unconf": 0.55,
        "sig_bull": 2.48,
        "sig_unconf": 2.20,
        "solo_c_coef": 0.33,
        "solo_c_cap": 0.45,
        "rev_t_501018": 0.48,
        "unconf_wt": 0.60,
        "taper_501018": 0.55,
        "mults": {"160723": 1.08, "161129": 0.75, "501018": 0.95, "162411": 1.15},
    },
    "中继下跌": {
        "cluster_bull": 0.90,
        "cluster_unconf": 0.48,
        "sig_bull": 2.50,
        "sig_unconf": 1.98,
        "solo_c_coef": 0.33,
        "solo_c_cap": 0.45,
        "rev_t_501018": 0.46,
        "unconf_wt": 0.52,
        "taper_501018": 0.50,
        "mults": {"160723": 1.08, "161129": 0.64, "501018": 0.94, "162411": 1.08},
    },
    "下降": {
        "cluster_bull": 0.86,
        "cluster_unconf": 0.42,
        "sig_bull": 2.40,
        "sig_unconf": 1.90,
        "solo_c_coef": 0.36,
        "solo_c_cap": 0.48,
        "rev_t_501018": 0.48,
        "unconf_wt": 0.48,
        "taper_501018": 0.58,
        "mults": {"160723": 0.95, "161129": 0.58, "501018": 1.18, "162411": 0.95},
    },
}

_DEGRADED_FALLBACK_PROBS: dict[str, float] = {
    "上升": 10.0,
    "震荡上行": 20.0,
    "震荡横盘": 45.0,
    "中继下跌": 15.0,
    "下降": 10.0,
}

_MIN_OIL_DAYS = 65
_REPLAY_WINDOW_DAYS = 120
_MAX_OIL_STALE_CALENDAR_DAYS = 10
_MAX_HO_LAG_CALENDAR_DAYS = 2
_MAX_XOP_DECAY_LAG_CALENDAR_DAYS = 3
_MAX_LIVE_BRN_AGE_SEC = 600.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _clean_yyyymmdd(raw: Any) -> str:
    txt = str(raw or "").replace("-", "").strip()[:8]
    if len(txt) == 8 and txt.isdigit():
        return txt
    return ""


def _calendar_day_gap(later_yyyymmdd: str, earlier_yyyymmdd: str) -> int | None:
    try:
        dt1 = datetime.strptime(later_yyyymmdd, "%Y%m%d")
        dt0 = datetime.strptime(earlier_yyyymmdd, "%Y%m%d")
        return (dt1 - dt0).days
    except ValueError:
        return None


def _parse_causal_daily_series(
    rows: Sequence[Sequence[Any]] | None,
    clean_td: str,
) -> tuple[list[str], list[float]]:
    """Parse `[date, ts, close, ...]` rows strictly filtering `date < clean_td`."""
    if not rows or not clean_td:
        return [], []
    dedup: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 3:
            continue
        d_str = _clean_yyyymmdd(row[0])
        if not d_str or d_str >= clean_td:
            continue
        try:
            close_px = float(row[2])
        except (TypeError, ValueError):
            continue
        if math.isfinite(close_px) and close_px > 0.0:
            dedup[d_str] = close_px
    if not dedup:
        return [], []
    ordered_dates = sorted(dedup)
    return ordered_dates, [dedup[d] for d in ordered_dates]


def _find_latest_index_on_or_before(sorted_dates: Sequence[str], target_date: str) -> int:
    """Binary search for largest index `j` with `sorted_dates[j] <= target_date`, or -1."""
    lo, hi = 0, len(sorted_dates) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if sorted_dates[mid] <= target_date:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _compute_daily_features(closes: Sequence[float], idx: int) -> tuple[dict[str, float], float, float]:
    """Compute the 16 dimensionless price-structure features at index `idx >= 60`."""
    c = float(closes[idx])
    ma5 = sum(closes[idx - 4 : idx + 1]) / 5.0
    ma15 = sum(closes[idx - 14 : idx + 1]) / 15.0
    ma20 = sum(closes[idx - 19 : idx + 1]) / 20.0
    ma60 = sum(closes[idx - 59 : idx + 1]) / 60.0
    ma20_prev5 = sum(closes[idx - 24 : idx - 4]) / 20.0

    w25 = closes[idx - 24 : idx + 1]
    hi25 = max(w25)
    lo25 = min(w25)
    lo10 = min(closes[idx - 9 : idx + 1])

    drets = [(closes[j] / closes[j - 1] - 1.0) for j in range(idx - 19, idx + 1)]
    path_pct = sum(abs(r) for r in drets)
    er20 = abs(c / closes[idx - 20] - 1.0) / path_pct if path_pct > 1e-12 else 0.0
    drets_pct = [r * 100.0 for r in drets]
    mean_ret = sum(drets_pct) / len(drets_pct)
    vol20 = math.sqrt(sum((r - mean_ret) ** 2 for r in drets_pct) / len(drets_pct))

    feats = {
        "px_ma15": (c / ma15 - 1.0) * 100.0,
        "px_ma20": (c / ma20 - 1.0) * 100.0,
        "px_ma60": (c / ma60 - 1.0) * 100.0,
        "ma5_ma15": (ma5 / ma15 - 1.0) * 100.0,
        "ma15_ma60": (ma15 / ma60 - 1.0) * 100.0,
        "ma20_slope5": (ma20 / ma20_prev5 - 1.0) * 100.0,
        "ret3": (c / closes[idx - 3] - 1.0) * 100.0,
        "ret5": (c / closes[idx - 5] - 1.0) * 100.0,
        "ret10": (c / closes[idx - 10] - 1.0) * 100.0,
        "ret20": (c / closes[idx - 20] - 1.0) * 100.0,
        "dd_hi25": (c / hi25 - 1.0) * 100.0,
        "plunge25": (lo10 / hi25 - 1.0) * 100.0,
        "reb_lo10": (c / lo10 - 1.0) * 100.0,
        "pos25": ((c - lo25) / (hi25 - lo25) * 100.0) if hi25 > lo25 else 50.0,
        "er20": er20,
        "vol20": vol20,
    }
    return feats, ma5, ma15


def _softmax_probs(feats: Mapping[str, float]) -> list[float]:
    """Evaluate frozen 5-class Softmax with 16 standardized features + 4 interactions."""
    z_vec = [
        _clip((float(feats[k]) - FROZEN_FEATURE_MEANS[k]) / max(FROZEN_FEATURE_STDS[k], 1e-9), -4.0, 4.0)
        for k in FROZEN_FEATURE_KEYS
    ]
    # 4 non-linear regime-structure interaction indicators
    ind_relay = 1.0 if (feats["plunge25"] <= -10.0 and feats["reb_lo10"] >= 2.0 and feats["ma5_ma15"] < 0.0) else 0.0
    ind_bull = 1.0 if (feats["ma5_ma15"] > 0.0 and feats["ma15_ma60"] > 0.0) else 0.0
    ind_bear = 1.0 if (feats["ma5_ma15"] < 0.0 and feats["ma15_ma60"] < 0.0) else 0.0
    ind_range = 1.0 if (abs(feats["px_ma20"]) < 3.0 and feats["er20"] < 0.25) else 0.0

    x_vec = [1.0, *z_vec, ind_relay, ind_bull, ind_bear, ind_range]
    logits = [
        sum(FROZEN_SOFTMAX_WEIGHTS[k][j] * x_vec[j] for j in range(len(x_vec)))
        for k in range(len(OIL_REGIMES))
    ]
    max_logit = max(logits)
    exps = [math.exp(v - max_logit) for v in logits]
    denom = sum(exps) or 1.0
    return [e / denom for e in exps]


def _normalize_probs(probs: Sequence[float]) -> list[float]:
    clipped = [max(0.0, float(v)) for v in probs]
    total = sum(clipped)
    if total <= 1e-12:
        return [0.2] * len(OIL_REGIMES)
    return [v / total for v in clipped]


def _format_probs_pct(probs: Sequence[float]) -> dict[str, float]:
    norm = _normalize_probs(probs)
    rounded = [round(v * 100.0, 2) for v in norm]
    diff = round(100.0 - sum(rounded), 2)
    if abs(diff) > 0.0:
        max_idx = max(range(len(rounded)), key=lambda idx: rounded[idx])
        rounded[max_idx] = round(max(0.0, rounded[max_idx] + diff), 2)
    return {OIL_REGIMES[idx]: rounded[idx] for idx in range(len(OIL_REGIMES))}


def _series_ret(closes: Sequence[float], idx: int, lag: int) -> float:
    return (float(closes[idx]) / float(closes[idx - lag]) - 1.0) * 100.0


def _series_ma(closes: Sequence[float], idx: int, win: int) -> float:
    return sum(float(closes[j]) for j in range(idx - win + 1, idx + 1)) / float(win)


def _series_ma5_ma15(closes: Sequence[float], idx: int) -> float:
    ma5 = _series_ma(closes, idx, 5)
    ma15 = _series_ma(closes, idx, 15)
    return (ma5 / ma15 - 1.0) * 100.0 if ma15 > 0.0 else 0.0


def _series_pos25(closes: Sequence[float], idx: int) -> float:
    w = closes[idx - 24 : idx + 1]
    hi = float(max(w))
    lo = float(min(w))
    c = float(closes[idx])
    return ((c - lo) / (hi - lo) * 100.0) if hi > lo else 50.0


def _extract_v62_dimension_features(
    bundle: Mapping[str, tuple[Sequence[str], Sequence[float]]],
    oil_idx: int,
) -> tuple[dict[str, dict[str, float] | None], dict[str, float]]:
    """Extract strictly causal (`d <= oil_dates[oil_idx]`) features for all 5 V6.2 dimensions."""
    oil_d, oil_c = bundle["oil_daily_2y"]
    cur_date = oil_d[oil_idx]

    c = float(oil_c[oil_idx])
    ma5 = _series_ma(oil_c, oil_idx, 5)
    ma15 = _series_ma(oil_c, oil_idx, 15)
    ma20 = _series_ma(oil_c, oil_idx, 20)
    ma60 = _series_ma(oil_c, oil_idx, 60)
    ma20_prev5 = _series_ma(oil_c, oil_idx - 5, 20)

    px_ma15 = (c / ma15 - 1.0) * 100.0
    px_ma20 = (c / ma20 - 1.0) * 100.0
    px_ma60 = (c / ma60 - 1.0) * 100.0
    ma5_ma15 = (ma5 / ma15 - 1.0) * 100.0
    ma15_ma60 = (ma15 / ma60 - 1.0) * 100.0
    ma20_slope5 = (ma20 / ma20_prev5 - 1.0) * 100.0

    brn_ret3 = _series_ret(oil_c, oil_idx, 3)
    brn_ret5 = _series_ret(oil_c, oil_idx, 5)
    brn_ret10 = _series_ret(oil_c, oil_idx, 10)
    brn_ret20 = _series_ret(oil_c, oil_idx, 20)

    w25 = oil_c[oil_idx - 24 : oil_idx + 1]
    hi25 = float(max(w25))
    lo25 = float(min(w25))
    lo10 = float(min(oil_c[oil_idx - 9 : oil_idx + 1]))
    dd_hi25 = (c / hi25 - 1.0) * 100.0
    plunge25 = (lo10 / hi25 - 1.0) * 100.0
    reb_lo10 = (c / lo10 - 1.0) * 100.0
    pos25 = ((c - lo25) / (hi25 - lo25) * 100.0) if hi25 > lo25 else 50.0

    drets20 = [(float(oil_c[j]) / float(oil_c[j - 1]) - 1.0) for j in range(oil_idx - 19, oil_idx + 1)]
    path_pct = sum(abs(r) for r in drets20)
    er20 = abs(c / float(oil_c[oil_idx - 20]) - 1.0) / path_pct if path_pct > 1e-12 else 0.0
    drets20_pct = [r * 100.0 for r in drets20]
    m20 = sum(drets20_pct) / len(drets20_pct)
    vol20 = math.sqrt(sum((r - m20) ** 2 for r in drets20_pct) / len(drets20_pct))

    drets5_pct = drets20_pct[-5:]
    m5 = sum(drets5_pct) / len(drets5_pct)
    vol5 = math.sqrt(sum((r - m5) ** 2 for r in drets5_pct) / len(drets5_pct))
    vol5_ratio = vol5 / max(vol20, 0.5)

    dim_feats: dict[str, dict[str, float] | None] = {}

    # 1. CTA Trend
    dim_feats["cta_trend"] = {
        "px_ma15": px_ma15,
        "px_ma20": px_ma20,
        "px_ma60": px_ma60,
        "ma5_ma15": ma5_ma15,
        "ma15_ma60": ma15_ma60,
        "ma20_slope5": ma20_slope5,
        "ret5": brn_ret5,
        "ret10": brn_ret10,
        "ret20": brn_ret20,
        "er20": er20,
        "ind_bull": 1.0 if (ma5 > ma15 and ma15 > ma60) else 0.0,
        "ind_bear": 1.0 if (ma5 < ma15 and ma15 < ma60) else 0.0,
    }

    # 2. Crack & Demand
    ho_d, ho_c = bundle.get("ho_daily_2y", ((), ()))
    cl_d, cl_c = bundle.get("cl_daily_2y", ((), ()))
    ng_d, ng_c = bundle.get("ng_daily_2y", ((), ()))
    ho_j = _find_latest_index_on_or_before(ho_d, cur_date)
    cl_j = _find_latest_index_on_or_before(cl_d, cur_date)
    ng_j = _find_latest_index_on_or_before(ng_d, cur_date)
    ho_gap = _calendar_day_gap(cur_date, ho_d[ho_j]) if ho_j >= 0 else None
    cl_gap = _calendar_day_gap(cur_date, cl_d[cl_j]) if cl_j >= 0 else None
    ng_gap = _calendar_day_gap(cur_date, ng_d[ng_j]) if ng_j >= 0 else None

    if (
        ho_j >= 25
        and ho_gap is not None
        and 0 <= ho_gap <= 4
        and cl_j >= 10
        and cl_gap is not None
        and 0 <= cl_gap <= 4
    ):
        ho_ret3 = _series_ret(ho_c, ho_j, 3)
        ho_ret10 = _series_ret(ho_c, ho_j, 10)
        ho_ma_diff = _series_ma5_ma15(ho_c, ho_j)
        ho_pos25 = _series_pos25(ho_c, ho_j)
        cl_ret5 = _series_ret(cl_c, cl_j, 5)
        ng_ma_diff = (
            _clip(_series_ma5_ma15(ng_c, ng_j), -25.0, 25.0)
            if (ng_j >= 15 and ng_gap is not None and 0 <= ng_gap <= 4)
            else 0.0
        )
        dim_feats["crack_demand"] = {
            "crack_ret3": ho_ret3 - brn_ret3,
            "crack_ret10": ho_ret10 - brn_ret10,
            "crack_ma_gap": ho_ma_diff - ma5_ma15,
            "ho_pos25": ho_pos25,
            "ho_ma5_ma15": ho_ma_diff,
            "wti_brn_ret5": _clip(cl_ret5 - brn_ret5, -15.0, 15.0),
            "ng_ma5_ma15": ng_ma_diff,
        }
    else:
        dim_feats["crack_demand"] = None

    # 3. Term Structure & Asia Physical (USO/BNO/DBO + SC0 + CL/BRN)
    uso_d, uso_c = bundle.get("uso_daily_2y", ((), ()))
    bno_d, bno_c = bundle.get("bno_daily_2y", ((), ()))
    dbo_d, dbo_c = bundle.get("dbo_daily_2y", ((), ()))
    sc0_d, sc0_c = bundle.get("sc0_daily_all", ((), ()))
    uso_j = _find_latest_index_on_or_before(uso_d, cur_date)
    bno_j = _find_latest_index_on_or_before(bno_d, cur_date)
    dbo_j = _find_latest_index_on_or_before(dbo_d, cur_date)
    sc0_j = _find_latest_index_on_or_before(sc0_d, cur_date)

    uso_gap = _calendar_day_gap(cur_date, uso_d[uso_j]) if uso_j >= 0 else None
    bno_gap = _calendar_day_gap(cur_date, bno_d[bno_j]) if bno_j >= 0 else None
    dbo_gap = _calendar_day_gap(cur_date, dbo_d[dbo_j]) if dbo_j >= 0 else None
    sc0_gap = _calendar_day_gap(cur_date, sc0_d[sc0_j]) if sc0_j >= 0 else None

    etf_ok = (
        uso_j >= 20
        and bno_j >= 20
        and dbo_j >= 20
        and uso_gap is not None
        and bno_gap is not None
        and dbo_gap is not None
        and 0 <= uso_gap <= 5
        and 0 <= bno_gap <= 5
        and 0 <= dbo_gap <= 5
    )
    sc0_ok = sc0_j >= 25 and sc0_gap is not None and 0 <= sc0_gap <= 5

    if etf_ok and cl_j >= 20 and cl_gap is not None and 0 <= cl_gap <= 5:
        roll_uso_dbo_5 = _series_ret(uso_c, uso_j, 5) - _series_ret(dbo_c, dbo_j, 5)
        roll_uso_dbo_20 = _series_ret(uso_c, uso_j, 20) - _series_ret(dbo_c, dbo_j, 20)
        roll_bno_dbo_10 = _series_ret(bno_c, bno_j, 10) - _series_ret(dbo_c, dbo_j, 10)

        ratios: list[float] = []
        for k in range(oil_idx - 19, oil_idx + 1):
            d_k = oil_d[k]
            cj = _find_latest_index_on_or_before(cl_d, d_k)
            if cj >= 0 and float(oil_c[k]) > 0.0:
                ratios.append(float(cl_c[cj]) / float(oil_c[k]))
        if len(ratios) >= 5:
            r_mean = sum(ratios) / len(ratios)
            r_std = math.sqrt(sum((x - r_mean) ** 2 for x in ratios) / len(ratios))
            cl_brn_ratio_z20 = _clip((ratios[-1] - r_mean) / max(r_std, 1e-4), -4.0, 4.0)
        else:
            cl_brn_ratio_z20 = 0.0

        if sc0_ok:
            sc_brn_ret5 = _clip(_series_ret(sc0_c, sc0_j, 5) - brn_ret5, -20.0, 20.0)
            sc_brn_ma_diff = _clip(_series_ma5_ma15(sc0_c, sc0_j) - ma5_ma15, -20.0, 20.0)
            sc0_pos25 = _series_pos25(sc0_c, sc0_j)
        else:
            sc_brn_ret5 = 0.0
            sc_brn_ma_diff = 0.0
            sc0_pos25 = 50.0

        dim_feats["term_physical"] = {
            "roll_uso_dbo_5": _clip(roll_uso_dbo_5, -15.0, 15.0),
            "roll_uso_dbo_20": _clip(roll_uso_dbo_20, -25.0, 25.0),
            "roll_bno_dbo_10": _clip(roll_bno_dbo_10, -20.0, 20.0),
            "sc_brn_ret5": sc_brn_ret5,
            "sc_brn_ma_diff": sc_brn_ma_diff,
            "sc0_pos25": sc0_pos25,
            "cl_brn_ratio_z20": cl_brn_ratio_z20,
        }
    else:
        dim_feats["term_physical"] = None

    # 4. Smart Money Equity (XOP / XLE / SPY / IWM)
    xop_d, xop_c = bundle.get("xop_daily_2y", ((), ()))
    xle_d, xle_c = bundle.get("xle_daily_2y", ((), ()))
    spy_d, spy_c = bundle.get("spy_daily_2y", ((), ()))
    iwm_d, iwm_c = bundle.get("iwm_daily_2y", ((), ()))
    xop_j = _find_latest_index_on_or_before(xop_d, cur_date)
    xle_j = _find_latest_index_on_or_before(xle_d, cur_date)
    spy_j = _find_latest_index_on_or_before(spy_d, cur_date)
    iwm_j = _find_latest_index_on_or_before(iwm_d, cur_date)

    xop_gap = _calendar_day_gap(cur_date, xop_d[xop_j]) if xop_j >= 0 else None
    xle_gap = _calendar_day_gap(cur_date, xle_d[xle_j]) if xle_j >= 0 else None
    spy_gap = _calendar_day_gap(cur_date, spy_d[spy_j]) if spy_j >= 0 else None
    iwm_gap = _calendar_day_gap(cur_date, iwm_d[iwm_j]) if iwm_j >= 0 else None

    if (
        xop_j >= 25
        and xle_j >= 20
        and spy_j >= 20
        and iwm_j >= 20
        and xop_gap is not None
        and xle_gap is not None
        and spy_gap is not None
        and iwm_gap is not None
        and 0 <= xop_gap <= 5
        and 0 <= xle_gap <= 5
        and 0 <= spy_gap <= 5
        and 0 <= iwm_gap <= 5
    ):
        xop_ret5 = _series_ret(xop_c, xop_j, 5)
        xop_ret20 = _series_ret(xop_c, xop_j, 20)
        xle_ret10 = _series_ret(xle_c, xle_j, 10)
        spy_ret5 = _series_ret(spy_c, spy_j, 5)
        spy_ret10 = _series_ret(spy_c, spy_j, 10)
        spy_ret20 = _series_ret(spy_c, spy_j, 20)
        iwm_ret10 = _series_ret(iwm_c, iwm_j, 10)
        dim_feats["smart_equity"] = {
            "xop_rs5": xop_ret5 - spy_ret5,
            "xop_rs20": xop_ret20 - spy_ret20,
            "xle_rs10": xle_ret10 - spy_ret10,
            "xop_vs_oil5": _clip(xop_ret5 - brn_ret5, -25.0, 25.0),
            "xop_ma5_ma15": _series_ma5_ma15(xop_c, xop_j),
            "xop_pos25": _series_pos25(xop_c, xop_j),
            "macro_risk_on": iwm_ret10 - spy_ret10,
        }
    else:
        dim_feats["smart_equity"] = None

    # 5. Mean Reversion & Volatility Extremes
    dim_feats["meanrev_vol"] = {
        "dd_hi25": dd_hi25,
        "plunge25": plunge25,
        "reb_lo10": reb_lo10,
        "pos25": pos25,
        "vol20": vol20,
        "vol5_ratio": _clip(vol5_ratio, 0.0, 5.0),
        "ret3": brn_ret3,
        "ind_relay": 1.0 if (plunge25 <= -10.0 and reb_lo10 >= 2.0 and ma5_ma15 < 0.0) else 0.0,
        "ind_oversold_exhaust": 1.0 if (brn_ret20 <= -15.0 and pos25 <= 18.0) else 0.0,
        "ind_overbought_exhaust": 1.0 if (brn_ret10 >= 10.0 and pos25 >= 90.0) else 0.0,
    }

    return dim_feats, {"ret3": brn_ret3, "ret5": brn_ret5}


def _predict_v62_dim_raw(dim_id: str, feats: Mapping[str, float]) -> list[float]:
    """Pure-Python Winsorized Z-score + temperature-scaled Softmax for one V6.2 dimension."""
    spec = FROZEN_V62_DIM_SPECS[dim_id]
    clips = spec["clips"]
    means = spec["means"]
    stds = spec["stds"]
    x_vec: list[float] = [1.0]
    for k in spec["cont_keys"]:
        lo, hi = clips[k]
        v = _clip(float(feats[k]), lo, hi)
        x_vec.append(_clip((v - means[k]) / stds[k], -3.5, 3.5))
    for k in spec["ind_keys"]:
        x_vec.append(float(feats[k]))
    temp = float(spec["temperature"])
    weights = spec["weights"]
    n_x = len(x_vec)
    logits = [
        sum(weights[c][j] * x_vec[j] for j in range(n_x)) / temp
        for c in range(len(OIL_REGIMES))
    ]
    max_logit = max(logits)
    exps = [math.exp(v - max_logit) for v in logits]
    denom = sum(exps) or 1.0
    return [e / denom for e in exps]


def _compute_v62_consensus_and_ci(
    dim_probs: Mapping[str, Sequence[float]],
    w_norm: Mapping[str, float],
    conformal_pad: Sequence[float] = FROZEN_V62_CONFORMAL_PAD,
) -> dict[str, Any]:
    """Compute cross-dimension consensus score, direction scores, and epistemic/conformal CIs."""
    dims = list(dim_probs.keys())
    p_bar = _normalize_probs([sum(w_norm[d] * float(dim_probs[d][k]) for d in dims) for k in range(5)])

    # 1) Weighted polarity vote agreement
    bull_p = {d: float(dim_probs[d][0]) + float(dim_probs[d][1]) for d in dims}
    bear_p = {d: float(dim_probs[d][3]) + float(dim_probs[d][4]) for d in dims}
    neut_p = {d: float(dim_probs[d][2]) for d in dims}
    pol_vote: dict[str, int] = {}
    for d in dims:
        if bull_p[d] >= bear_p[d] and bull_p[d] >= neut_p[d]:
            pol_vote[d] = 1
        elif bear_p[d] >= bull_p[d] and bear_p[d] >= neut_p[d]:
            pol_vote[d] = -1
        else:
            pol_vote[d] = 0
    w_bull_vote = sum(w_norm[d] for d in dims if pol_vote[d] == 1)
    w_bear_vote = sum(w_norm[d] for d in dims if pol_vote[d] == -1)
    w_neut_vote = sum(w_norm[d] for d in dims if pol_vote[d] == 0)
    vote_agree = max(w_bull_vote, w_bear_vote, w_neut_vote)
    a_vote = _clip((vote_agree - 0.34) / 0.66, 0.0, 1.0)

    # 2) Direction score dispersion across dimensions
    d_scores = {
        d: (float(dim_probs[d][0]) + 0.65 * float(dim_probs[d][1]))
        - (0.70 * float(dim_probs[d][3]) + float(dim_probs[d][4]))
        for d in dims
    }
    d_bar = sum(w_norm[d] * d_scores[d] for d in dims)
    sd_d = math.sqrt(max(0.0, sum(w_norm[d] * (d_scores[d] - d_bar) ** 2 for d in dims)))
    a_disp = _clip(1.0 - 2.2 * sd_d, 0.0, 1.0)

    # 3) Ensemble conviction sharpness (1 - normalized entropy)
    ent_bar = -sum(p * math.log(max(p, 1e-12)) for p in p_bar)
    a_sharp = _clip(1.0 - ent_bar / math.log(5.0), 0.0, 1.0)

    consensus_pct = round(100.0 * (0.45 * a_vote + 0.35 * a_disp + 0.20 * a_sharp), 1)
    q33 = FROZEN_V62_OOF_CONS_QUANTILES["q33"]
    q67 = FROZEN_V62_OOF_CONS_QUANTILES["q67"]
    consensus_band = "高置信共振" if consensus_pct >= q67 else ("中等共识" if consensus_pct >= q33 else "多空分歧")

    ci_epistemic_lo: list[float] = []
    ci_epistemic_hi: list[float] = []
    ci_conformal_lo: list[float] = []
    ci_conformal_hi: list[float] = []
    env_min: list[float] = []
    env_max: list[float] = []
    for k in range(5):
        vs = [float(dim_probs[d][k]) for d in dims]
        sd_k = math.sqrt(max(0.0, sum(w_norm[d] * (float(dim_probs[d][k]) - p_bar[k]) ** 2 for d in dims)))
        e_lo = _clip(p_bar[k] - 1.2816 * sd_k, 0.0, p_bar[k])
        e_hi = _clip(p_bar[k] + 1.2816 * sd_k, p_bar[k], 1.0)
        c_lo = _clip(e_lo - float(conformal_pad[k]), 0.0, p_bar[k])
        c_hi = _clip(e_hi + float(conformal_pad[k]), p_bar[k], 1.0)
        ci_epistemic_lo.append(e_lo)
        ci_epistemic_hi.append(e_hi)
        ci_conformal_lo.append(c_lo)
        ci_conformal_hi.append(c_hi)
        env_min.append(min(vs))
        env_max.append(max(vs))

    return {
        "p_bar": tuple(p_bar),
        "d_scores": dict(d_scores),
        "d_bar": float(d_bar),
        "w_bull_vote": round(w_bull_vote, 4),
        "w_bear_vote": round(w_bear_vote, 4),
        "consensus_pct": float(consensus_pct),
        "consensus_band": consensus_band,
        "ci_epistemic_lo": tuple(ci_epistemic_lo),
        "ci_epistemic_hi": tuple(ci_epistemic_hi),
        "ci_conformal_lo": tuple(ci_conformal_lo),
        "ci_conformal_hi": tuple(ci_conformal_hi),
        "env_min": tuple(env_min),
        "env_max": tuple(env_max),
    }


def _apply_golden_cross_reweight(probs: Sequence[float], g_crude: float, ret5: float) -> list[float]:
    interp_p = list(probs)
    if g_crude > 0.0 and ret5 > 0.0:
        cut = interp_p[3] * 0.70 * g_crude
        interp_p[3] = max(0.0, interp_p[3] - cut)
        up_sum = interp_p[0] + interp_p[1] + interp_p[2]
        if up_sum > 1e-12:
            for k in (0, 1, 2):
                interp_p[k] += cut * (interp_p[k] / up_sum)
        else:
            for k in (0, 1, 2):
                interp_p[k] += cut / 3.0
        interp_p = _normalize_probs(interp_p)
    return interp_p


def _compute_regime_table_params(
    interp_p: Sequence[float],
    g_crude: float,
) -> tuple[float, float, dict[str, float], float, float, dict[str, float]]:
    cluster_bull = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["cluster_bull"]) for k in range(5))
    cluster_unconf = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["cluster_unconf"]) for k in range(5))
    sig_bull = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["sig_bull"]) for k in range(5))
    sig_unconf = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["sig_unconf"]) for k in range(5))
    solo_c_coef = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["solo_c_coef"]) for k in range(5))
    solo_c_cap = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["solo_c_cap"]) for k in range(5))
    rev_t_501018 = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["rev_t_501018"]) for k in range(5))
    unconf_wt = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["unconf_wt"]) for k in range(5))
    taper_501018 = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["taper_501018"]) for k in range(5))

    mult_syms = tuple(V5_REGIME_TABLE["震荡横盘"]["mults"].keys())
    mults = {
        sym: sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["mults"][sym]) for k in range(5))
        for sym in mult_syms
    }
    cluster_eff = (1.0 - g_crude) * cluster_unconf + g_crude * cluster_bull
    sigma_eff = (1.0 - g_crude) * sig_unconf + g_crude * sig_bull
    hints = {
        "rev_t_501018": rev_t_501018,
        "unconf_wt": unconf_wt,
        "taper_501018": taper_501018,
    }
    return cluster_eff, sigma_eff, mults, solo_c_coef, solo_c_cap, hints


def _build_ci_by_regime(
    display_probs_pct: Mapping[str, float],
    base: Mapping[str, Any],
) -> dict[str, dict[str, float]]:
    v62_p_bar = base["v62_p_bar"]
    ep_lo = base["v62_ci_epistemic_lo"]
    ep_hi = base["v62_ci_epistemic_hi"]
    cf_lo = base["v62_ci_conformal_lo"]
    cf_hi = base["v62_ci_conformal_hi"]
    env_min = base["v62_env_min"]
    env_max = base["v62_env_max"]
    out: dict[str, dict[str, float]] = {}
    for k, reg in enumerate(OIL_REGIMES):
        mean_pct = round(_clip(float(display_probs_pct[reg]), 0.0, 100.0), 2)
        comm_mean_pct = round(_clip(float(v62_p_bar[k]) * 100.0, 0.0, 100.0), 2)
        ci_lo = round(_clip(min(float(ep_lo[k]) * 100.0, mean_pct), 0.0, 100.0), 2)
        ci_hi = round(_clip(max(float(ep_hi[k]) * 100.0, mean_pct), 0.0, 100.0), 2)
        if ci_lo > mean_pct:
            ci_lo = mean_pct
        if ci_hi < mean_pct:
            ci_hi = mean_pct
        conf_lo = round(_clip(min(float(cf_lo[k]) * 100.0, ci_lo), 0.0, 100.0), 2)
        conf_hi = round(_clip(max(float(cf_hi[k]) * 100.0, ci_hi), 0.0, 100.0), 2)
        if conf_lo > ci_lo:
            conf_lo = ci_lo
        if conf_hi < ci_hi:
            conf_hi = ci_hi
        out[reg] = {
            "mean_pct": mean_pct,
            "committee_mean_pct": comm_mean_pct,
            "ci_low_pct": ci_lo,
            "ci_high_pct": ci_hi,
            "conformal_90_low_pct": conf_lo,
            "conformal_90_high_pct": conf_hi,
            "min_pct": round(_clip(float(env_min[k]) * 100.0, 0.0, 100.0), 1),
            "max_pct": round(_clip(float(env_max[k]) * 100.0, 0.0, 100.0), 1),
        }
    return out


def _build_dimension_votes(base: Mapping[str, Any]) -> list[dict[str, Any]]:
    dim_probs: Mapping[str, Sequence[float]] = base["v62_dim_probs"]
    w_norm: Mapping[str, float] = base["v62_w_norm"]
    d_scores: Mapping[str, float] = base["v62_d_scores"]
    votes: list[dict[str, Any]] = []
    for d_id in V62_DIMENSION_ORDER:
        if d_id not in dim_probs:
            continue
        probs = dim_probs[d_id]
        best_k = max(range(len(OIL_REGIMES)), key=lambda k: probs[k])
        votes.append(
            {
                "id": d_id,
                "label": V62_DIMENSION_SHORT_LABELS[d_id],
                "weight_pct": round(float(w_norm[d_id]) * 100.0, 1),
                "vote_regime": OIL_REGIMES[best_k],
                "vote_prob_pct": round(float(probs[best_k]) * 100.0, 1),
                "dir_score": round(float(d_scores[d_id]), 3),
                "probs": _format_probs_pct(probs),
            }
        )
    return votes


class OilRegimeRouter:
    """Thread-safe causal five-state crude oil regime router with stateless replay."""

    def __init__(self, *, max_cache_entries: int = 32) -> None:
        self._lock = RLock()
        self._max_cache_entries = max(4, int(max_cache_entries))
        self._base_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

    def clear_cache(self) -> None:
        with self._lock:
            self._base_cache.clear()

    @staticmethod
    def _load_macro_series(
        research_store: Any,
        clean_td: str,
    ) -> tuple[list[str], list[float], list[str], list[float], list[str], list[float]]:
        if research_store is None or not clean_td:
            return [], [], [], [], [], []
        macro = V9MacroDataStore(research_store, allow_network=False)
        try:
            oil_rows = macro.load_oil_daily_rows()
        except Exception:
            oil_rows = []
        try:
            ho_rows = macro.load_ho_daily_rows()
        except Exception:
            ho_rows = []
        try:
            xop_rows = macro.load_xop_daily_rows()
        except Exception:
            xop_rows = []
        oil_d, oil_c = _parse_causal_daily_series(oil_rows, clean_td)
        ho_d, ho_c = _parse_causal_daily_series(ho_rows, clean_td)
        xop_d, xop_c = _parse_causal_daily_series(xop_rows, clean_td)
        return oil_d, oil_c, ho_d, ho_c, xop_d, xop_c

    @classmethod
    def _load_v62_macro_bundle(
        cls,
        research_store: Any,
        clean_td: str,
    ) -> dict[str, tuple[list[str], list[float]]]:
        """Read-only zero-network loader for all 12 causal (`< clean_td`) V6.2 macro series."""
        bundle: dict[str, tuple[list[str], list[float]]] = {k: ([], []) for k in V62_MACRO_CACHE_KEYS}
        if research_store is None or not clean_td:
            return bundle

        conn = getattr(research_store, "_connection", None)
        if conn is None:
            oil_d, oil_c, ho_d, ho_c, xop_d, xop_c = cls._load_macro_series(research_store, clean_td)
            bundle["oil_daily_2y"] = (oil_d, oil_c)
            bundle["ho_daily_2y"] = (ho_d, ho_c)
            bundle["xop_daily_2y"] = (xop_d, xop_c)
            return bundle

        lock = getattr(research_store, "_lock", None)
        placeholders = ",".join("?" for _ in V62_MACRO_CACHE_KEYS)
        sql = f"SELECT cache_key, payload_json FROM quant_macro_cache WHERE cache_key IN ({placeholders})"
        raw_rows: list[tuple[Any, Any]] = []
        try:
            if lock is not None:
                with lock:
                    raw_rows = list(conn.execute(sql, V62_MACRO_CACHE_KEYS).fetchall())
            else:
                raw_rows = list(conn.execute(sql, V62_MACRO_CACHE_KEYS).fetchall())
        except Exception:
            return bundle

        for key, payload_json in raw_rows:
            k_str = str(key or "")
            if k_str not in bundle or not payload_json:
                continue
            try:
                parsed = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes)):
                bundle[k_str] = _parse_causal_daily_series(parsed, clean_td)
        return bundle

    def peek_reference_dates(
        self,
        research_store: Any,
        *,
        trade_date: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Return latest causal (`< trade_date`) dates for `(oil, ho, xop)` for cache keying."""
        clean_td = _clean_yyyymmdd(trade_date)
        oil_d, _, ho_d, _, xop_d, _ = self._load_macro_series(research_store, clean_td)
        return (
            oil_d[-1] if oil_d else None,
            ho_d[-1] if ho_d else None,
            xop_d[-1] if xop_d else None,
        )

    @staticmethod
    def _build_degraded_result(
        *,
        enforced: bool,
        profile: str,
        oil_reference_date: str | None = None,
        oil_close_t1: float | None = None,
        degraded_mode: str = "oil_daily_unavailable",
        degraded_sources: Sequence[str] = ("oil_daily",),
    ) -> dict[str, Any]:
        neutral_tbl = V5_REGIME_TABLE["震荡横盘"]
        return {
            "enforced": bool(enforced),
            "profile": profile,
            "oil_reference_date": oil_reference_date,
            "oil_close_t1": round(float(oil_close_t1), 4) if oil_close_t1 is not None else None,
            "locked_regime": "震荡横盘",
            "effective_regime": "震荡横盘",
            "probs": dict(_DEGRADED_FALLBACK_PROBS),
            "ma_confirm_ratio": 0.0,
            "effective_cluster_cap_pct": 55.0,
            "effective_sigma_cap_pct": 2.40,
            "solo_crude_coef": float(neutral_tbl["solo_c_coef"]),
            "solo_crude_cap": float(neutral_tbl["solo_c_cap"]),
            "symbol_multipliers": {str(k): float(v) for k, v in neutral_tbl["mults"].items()},
            "symbol_tuning_hints": {
                "rev_t_501018": float(neutral_tbl["rev_t_501018"]),
                "unconf_wt": float(neutral_tbl["unconf_wt"]),
                "taper_501018": float(neutral_tbl["taper_501018"]),
            },
            "fast_crash_active": False,
            "live_brn_price": None,
            "live_brn_chg_pct": None,
            "live_brn_fresh": False,
            "next_day_preview": None,
            "degraded_mode": degraded_mode,
            "degraded_sources": list(dict.fromkeys(str(s) for s in degraded_sources if str(s))),
            "consensus_pct": None,
            "consensus_band": None,
            "ci_guard_action": "neutral",
            "active_dimensions": [],
            "missing_dimensions": list(V62_DIMENSION_ORDER),
            "ci_by_regime": None,
            "dimension_votes": [],
        }

    @staticmethod
    def _replay_daily_base(
        *,
        oil_dates: Sequence[str],
        oil_closes: Sequence[float],
        ho_dates: Sequence[str],
        ho_closes: Sequence[float],
        xop_dates: Sequence[str],
        xop_closes: Sequence[float],
        macro_bundle: Mapping[str, tuple[Sequence[str], Sequence[float]]] | None = None,
    ) -> dict[str, Any]:
        """Run deterministic 120-day stateless replay (V5 anchor + V6.2 5-dim committee) ending at `oil_dates[-1]`."""
        n_oil = len(oil_closes)
        start_idx = max(60, n_oil - _REPLAY_WINDOW_DAYS)

        bundle: dict[str, tuple[Sequence[str], Sequence[float]]] = {
            k: ((), ()) for k in V62_MACRO_CACHE_KEYS
        }
        if macro_bundle is not None:
            for k in V62_MACRO_CACHE_KEYS:
                if k in macro_bundle:
                    bundle[k] = macro_bundle[k]
        bundle["oil_daily_2y"] = (oil_dates, oil_closes)
        bundle["ho_daily_2y"] = (ho_dates, ho_closes)
        bundle["xop_daily_2y"] = (xop_dates, xop_closes)

        sm_p: list[float] | None = None
        locked_idx = 2  # Start from neutral "震荡横盘"
        last_feats: dict[str, float] = {}
        last_ma5 = float(oil_closes[-1])
        last_ma15 = float(oil_closes[-1])
        last_crack3 = 0.0
        last_degraded: list[str] = []

        v62_sm_by_dim: dict[str, list[float] | None] = {d: None for d in V62_DIMENSION_ORDER}
        last_v62_avail: dict[str, tuple[float, ...]] = {}
        last_v62_w_norm: dict[str, float] = {}
        last_v62_agg: dict[str, Any] = {}

        for idx in range(start_idx, n_oil):
            cur_date = oil_dates[idx]
            feats, ma5, ma15 = _compute_daily_features(oil_closes, idx)
            p = _softmax_probs(feats)
            step_degraded: list[str] = []
            crack3 = 0.0

            # Tier 1 auxiliary source 1: Heating Oil crack spread (lag <= 2 calendar days)
            ho_j = _find_latest_index_on_or_before(ho_dates, cur_date)
            ho_ok = False
            if ho_j >= 3:
                ho_gap = _calendar_day_gap(cur_date, ho_dates[ho_j])
                if ho_gap is not None and 0 <= ho_gap <= _MAX_HO_LAG_CALENDAR_DAYS:
                    ho_ret3 = (ho_closes[ho_j] / ho_closes[ho_j - 3] - 1.0) * 100.0
                    crack3 = ho_ret3 - feats["ret3"]
                    ho_ok = True
                    if crack3 < -2.0:
                        p[0] *= 0.82
                        p[3] *= 1.18
                        p = _normalize_probs(p)
                    elif crack3 > 2.0:
                        p[1] *= 1.15
                        p[3] *= 0.85
                        p = _normalize_probs(p)
            if not ho_ok:
                step_degraded.append("ho_daily")

            # Tier 1 auxiliary source 2: XOP 5-day relative strength (1..3d lag -> 0.5x decay, >4d or missing -> 0)
            xop_j = _find_latest_index_on_or_before(xop_dates, cur_date)
            xop_decay = 0.0
            if xop_j >= 5:
                xop_gap = _calendar_day_gap(cur_date, xop_dates[xop_j])
                if xop_gap is not None:
                    if xop_gap == 0:
                        xop_decay = 1.0
                    elif 1 <= xop_gap <= _MAX_XOP_DECAY_LAG_CALENDAR_DAYS:
                        xop_decay = 0.5
                    elif xop_gap <= 4:
                        xop_decay = 0.5
                    else:
                        step_degraded.append("xop_daily")
                else:
                    step_degraded.append("xop_daily")
            else:
                step_degraded.append("xop_daily")

            if xop_decay > 0.0 and xop_j >= 5:
                xop_ret5 = (xop_closes[xop_j] / xop_closes[xop_j - 5] - 1.0) * 100.0
                xop_rel5 = (xop_ret5 - feats["ret5"]) * xop_decay
                if xop_rel5 > 3.0:
                    p[1] *= 1.0 + 0.10 * xop_decay
                    p[4] *= max(0.5, 1.0 - 0.10 * xop_decay)
                    p = _normalize_probs(p)
                elif xop_rel5 < -3.0:
                    p[0] *= max(0.5, 1.0 - 0.10 * xop_decay)
                    p[3] *= 1.0 + 0.10 * xop_decay
                    p = _normalize_probs(p)

            # Dual-speed EWMA posterior smoothing (V5 anchor)
            alpha = 0.90 if abs(feats["ret3"]) >= 4.0 else 0.65
            if sm_p is None:
                sm_p = list(p)
            else:
                sm_p = _normalize_probs([alpha * p[k] + (1.0 - alpha) * sm_p[k] for k in range(len(OIL_REGIMES))])

            # Asymmetric hysteresis state lock (V5 structural anchor)
            best_idx = max(range(len(OIL_REGIMES)), key=lambda k: sm_p[k])
            if best_idx != locked_idx:
                if best_idx in (3, 4) and locked_idx not in (3, 4):
                    threshold = 0.03
                elif locked_idx in (3, 4) and best_idx not in (3, 4):
                    threshold = 0.07
                else:
                    threshold = 0.05
                if (sm_p[best_idx] - sm_p[locked_idx]) >= threshold:
                    locked_idx = best_idx

            # V6.2 5-Dimension Committee causal step
            dim_feats, v62_aux = _extract_v62_dimension_features(bundle, idx)
            v62_alpha = 0.88 if abs(v62_aux["ret3"]) >= 4.0 else 0.65
            step_avail: dict[str, list[float]] = {}
            for d_id in V62_DIMENSION_ORDER:
                f_dim = dim_feats.get(d_id)
                if f_dim is None:
                    continue
                p_raw = _predict_v62_dim_raw(d_id, f_dim)
                prev_dim = v62_sm_by_dim[d_id]
                p_sm = (
                    list(p_raw)
                    if prev_dim is None
                    else _normalize_probs(
                        [v62_alpha * p_raw[k] + (1.0 - v62_alpha) * prev_dim[k] for k in range(len(OIL_REGIMES))]
                    )
                )
                v62_sm_by_dim[d_id] = p_sm
                step_avail[d_id] = p_sm

            if idx == n_oil - 1:
                last_feats = feats
                last_ma5 = ma5
                last_ma15 = ma15
                last_crack3 = crack3
                last_degraded = step_degraded
                w_sum = sum(FROZEN_V62_PRIOR_WEIGHTS[d] for d in step_avail) or 1.0
                last_v62_w_norm = {d: FROZEN_V62_PRIOR_WEIGHTS[d] / w_sum for d in step_avail}
                last_v62_avail = {d: tuple(ps) for d, ps in step_avail.items()}
                last_v62_agg = _compute_v62_consensus_and_ci(
                    step_avail, last_v62_w_norm, FROZEN_V62_CONFORMAL_PAD
                )

        assert sm_p is not None
        active_dimensions = tuple(d for d in V62_DIMENSION_ORDER if d in last_v62_avail)
        missing_dimensions = tuple(d for d in V62_DIMENSION_ORDER if d not in last_v62_avail)
        return {
            "base_probs": tuple(sm_p),
            "locked_idx": locked_idx,
            "oil_reference_date": str(oil_dates[-1]),
            "oil_close_t1": float(oil_closes[-1]),
            "oil_ma5": float(last_ma5),
            "oil_ma15": float(last_ma15),
            "last_feats": dict(last_feats),
            "last_crack3": float(last_crack3),
            "degraded_sources": tuple(last_degraded),
            "v62_p_bar": last_v62_agg["p_bar"],
            "v62_consensus_pct": float(last_v62_agg["consensus_pct"]),
            "v62_consensus_band": str(last_v62_agg["consensus_band"]),
            "v62_d_scores": dict(last_v62_agg["d_scores"]),
            "v62_d_bar": float(last_v62_agg["d_bar"]),
            "v62_w_bull_vote": float(last_v62_agg["w_bull_vote"]),
            "v62_w_bear_vote": float(last_v62_agg["w_bear_vote"]),
            "v62_ci_epistemic_lo": last_v62_agg["ci_epistemic_lo"],
            "v62_ci_epistemic_hi": last_v62_agg["ci_epistemic_hi"],
            "v62_ci_conformal_lo": last_v62_agg["ci_conformal_lo"],
            "v62_ci_conformal_hi": last_v62_agg["ci_conformal_hi"],
            "v62_env_min": last_v62_agg["env_min"],
            "v62_env_max": last_v62_agg["env_max"],
            "v62_dim_probs": last_v62_avail,
            "v62_w_norm": last_v62_w_norm,
            "active_dimensions": active_dimensions,
            "missing_dimensions": missing_dimensions,
        }

    @staticmethod
    def _resolve_policy_from_base(
        base: Mapping[str, Any],
        *,
        g_crude: float,
        clean_profile: str,
    ) -> dict[str, Any]:
        """Resolve V5/V6.2 hybrid caps, multipliers, CI guardrails, and overbought taper from daily base."""
        v5_probs = list(base["base_probs"])
        locked_idx = int(base["locked_idx"])
        locked_regime = OIL_REGIMES[locked_idx]
        feats = base["last_feats"]
        crack3 = float(base["last_crack3"])
        ret5 = float(feats.get("ret5", 0.0))

        # 1) V5 anchor parameters
        interp_p_v5 = _apply_golden_cross_reweight(v5_probs, g_crude, ret5)
        cc_v5, sig_v5, mults_v5, solo_c_coef, solo_c_cap, hints = _compute_regime_table_params(
            interp_p_v5, g_crude
        )

        # 2) P4 dual-layer hybrid fusion + OOF CI guardrails (when >= 3 dims available & not legacy profile)
        active_dims: Sequence[str] = base["active_dimensions"]
        ci_guard_action = "neutral"
        if len(active_dims) >= 3 and clean_profile != "oil_v5_legacy":
            v62_p_bar = list(base["v62_p_bar"])
            interp_p_v62 = _apply_golden_cross_reweight(v62_p_bar, g_crude, ret5)
            cc_v62, sig_v62, mults_v62, _, _, _ = _compute_regime_table_params(interp_p_v62, g_crude)

            base_probs = _normalize_probs([0.65 * v5_probs[k] + 0.35 * v62_p_bar[k] for k in range(5)])
            cluster_eff = 0.65 * cc_v5 + 0.35 * cc_v62
            sigma_eff = 0.65 * sig_v5 + 0.35 * sig_v62
            mults_eff = {sym: 0.65 * mults_v5[sym] + 0.35 * mults_v62[sym] for sym in mults_v5}

            q33 = FROZEN_V62_OOF_CONS_QUANTILES["q33"]
            q67 = FROZEN_V62_OOF_CONS_QUANTILES["q67"]
            cons = float(base["v62_consensus_pct"])
            d_bar = float(base["v62_d_bar"])
            d_scores: Mapping[str, float] = base["v62_d_scores"]

            if locked_regime in ("中继下跌", "下降"):
                if cons >= q67 and d_bar <= -0.15 and d_scores.get("crack_demand", 0.0) < -0.05:
                    cluster_eff = max(0.38, cluster_eff * 0.91)
                    sigma_eff = max(1.80, sigma_eff * 0.95)
                    ci_guard_action = "bear_high_conf_compress"
                elif cons < q33 and (
                    d_scores.get("term_physical", -1.0) > 0.02 or d_scores.get("meanrev_vol", -1.0) > 0.05
                ):
                    cluster_eff = min(0.56, max(cluster_eff, cluster_eff + 0.04))
                    mults_eff["160723"] = min(1.25, mults_eff["160723"] * 1.05)
                    mults_eff["501018"] = min(1.25, mults_eff["501018"] * 1.05)
                    ci_guard_action = "bear_divergence_support"
            elif g_crude >= 0.35 and cons >= q67 and d_bar >= 0.15 and d_scores.get("crack_demand", 0.0) >= 0.0:
                cluster_eff = min(0.96, max(cluster_eff, cluster_eff + 0.03))
                sigma_eff = min(2.72, max(sigma_eff, sigma_eff + 0.08))
                ci_guard_action = "bull_high_conf_unlock"
        else:
            base_probs = list(v5_probs)
            cluster_eff = cc_v5
            sigma_eff = sig_v5
            mults_eff = dict(mults_v5)

        # 3) Overbought momentum-blunting taper ALWAYS executes AFTER hybrid blend & CI guardrails
        pos25 = float(feats.get("pos25", 50.0))
        ret10 = float(feats.get("ret10", 0.0))
        ret3 = float(feats.get("ret3", 0.0))
        vol20 = float(feats.get("vol20", 2.0))
        if pos25 >= 95.0 and ret10 >= 11.0 and (ret3 <= 1.0 or vol20 >= 3.2 or crack3 <= -2.0):
            sigma_eff = min(sigma_eff, 2.25)
            cluster_eff = min(cluster_eff, 0.82)

        return {
            "base_probs": base_probs,
            "cluster_eff": cluster_eff,
            "sigma_eff": sigma_eff,
            "mults_eff": mults_eff,
            "solo_c_coef": solo_c_coef,
            "solo_c_cap": solo_c_cap,
            "hints": hints,
            "ci_guard_action": ci_guard_action,
        }

    def evaluate_regime(
        self,
        research_store: Any,
        *,
        trade_date: str,
        profile: str = "oil_v5_dual_key",
        symbol_ma_map: Mapping[str, tuple[float, float]] | None = None,
        live_brn_price: float | None = None,
        live_brn_age_sec: float | None = None,
        require_fresh_live_brn: bool = True,
        enforced: bool = False,
    ) -> dict[str, Any]:
        """Evaluate causal oil regime, V6.2 5-dim committee CI guardrails, and optional read-only Fast Crash overlay."""
        clean_td = _clean_yyyymmdd(trade_date)
        clean_profile = str(profile or "oil_v5_dual_key").strip()[:64] or "oil_v5_dual_key"

        if research_store is None or not clean_td:
            return self._build_degraded_result(enforced=enforced, profile=clean_profile)

        macro_bundle = self._load_v62_macro_bundle(research_store, clean_td)
        oil_dates, oil_closes = macro_bundle["oil_daily_2y"]
        ho_dates, ho_closes = macro_bundle["ho_daily_2y"]
        xop_dates, xop_closes = macro_bundle["xop_daily_2y"]
        oil_last_date = oil_dates[-1] if oil_dates else None
        oil_close_t1 = oil_closes[-1] if oil_closes else None

        # Tier 2: primary oil_daily missing, too short (< 65), or stale (> 10 calendar days)
        stale_gap = _calendar_day_gap(clean_td, oil_last_date) if oil_last_date else None
        if (
            len(oil_closes) < _MIN_OIL_DAYS
            or oil_last_date is None
            or stale_gap is None
            or stale_gap > _MAX_OIL_STALE_CALENDAR_DAYS
        ):
            return self._build_degraded_result(
                enforced=enforced,
                profile=clean_profile,
                oil_reference_date=oil_last_date,
                oil_close_t1=oil_close_t1,
                degraded_mode="oil_daily_unavailable",
                degraded_sources=("oil_daily",),
            )

        cache_key = (
            clean_td,
            clean_profile,
            *(macro_bundle[k][0][-1] if macro_bundle[k][0] else None for k in V62_MACRO_CACHE_KEYS),
        )
        with self._lock:
            base = self._base_cache.get(cache_key)
            if base is not None:
                self._base_cache.move_to_end(cache_key)

        if base is None:
            base = self._replay_daily_base(
                oil_dates=oil_dates,
                oil_closes=oil_closes,
                ho_dates=ho_dates,
                ho_closes=ho_closes,
                xop_dates=xop_dates,
                xop_closes=xop_closes,
                macro_bundle=macro_bundle,
            )
            with self._lock:
                self._base_cache[cache_key] = base
                self._base_cache.move_to_end(cache_key)
                while len(self._base_cache) > self._max_cache_entries:
                    self._base_cache.popitem(last=False)

        # Read-only copy from cached daily base state (never mutates `base`)
        locked_idx = int(base["locked_idx"])
        locked_regime = OIL_REGIMES[locked_idx]
        effective_regime = locked_regime
        oil_ma5 = float(base["oil_ma5"])
        oil_ma15 = float(base["oil_ma15"])
        ref_close_t1 = float(base["oil_close_t1"])
        degraded_sources = list(base["degraded_sources"])

        # Dual-key continuous MA confirmation `g_crude` (crude cluster only; explicitly ignores 162411)
        oil_ratio = (oil_ma5 / oil_ma15) if oil_ma15 > 0.0 else 1.0
        crude_ratios: list[float] = []
        if isinstance(symbol_ma_map, Mapping):
            for sym, pair in symbol_ma_map.items():
                clean_sym = str(sym or "").strip().upper()
                if clean_sym not in CRUDE_CLUSTER_SYMBOLS:
                    continue
                if isinstance(pair, Sequence) and not isinstance(pair, (str, bytes)) and len(pair) >= 2:
                    try:
                        s_ma5 = float(pair[0])
                        s_ma15 = float(pair[1])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(s_ma5) and math.isfinite(s_ma15) and s_ma5 > 0.0 and s_ma15 > 0.0:
                        crude_ratios.append(s_ma5 / s_ma15)
        eff_ratio = min(oil_ratio, float(median(crude_ratios))) if crude_ratios else oil_ratio
        g_crude = _clip((eff_ratio - 0.998) / 0.012, 0.0, 1.0)

        policy = self._resolve_policy_from_base(base, g_crude=g_crude, clean_profile=clean_profile)
        display_p = list(policy["base_probs"])
        cluster_eff = float(policy["cluster_eff"])
        sigma_eff = float(policy["sigma_eff"])
        solo_c_coef = float(policy["solo_c_coef"])
        solo_c_cap = float(policy["solo_c_cap"])
        hints = policy["hints"]
        ci_guard_action = str(policy["ci_guard_action"])
        symbol_multipliers = {sym: round(float(val), 4) for sym, val in policy["mults_eff"].items()}

        # Tier 3: Read-only intraday Fast Crash ramp overlay (ALWAYS executes after hybrid blend & CI guardrails)
        fast_crash_active = False
        live_brn_price_out: float | None = None
        live_brn_chg_pct_out: float | None = None
        live_brn_fresh_out = False
        next_day_preview: dict[str, Any] | None = None
        if live_brn_price is not None:
            try:
                live_px = float(live_brn_price)
            except (TypeError, ValueError):
                live_px = 0.0
            valid_live_px = math.isfinite(live_px) and live_px > 0.0
            age_ok = True
            if live_brn_age_sec is not None:
                try:
                    age_ok = float(live_brn_age_sec) <= _MAX_LIVE_BRN_AGE_SEC
                except (TypeError, ValueError):
                    age_ok = False
            if valid_live_px:
                live_brn_price_out = round(live_px, 4)
                live_brn_chg_pct_out = (
                    round((live_px / ref_close_t1 - 1.0) * 100.0, 2) if ref_close_t1 > 0.0 else None
                )
                live_brn_fresh_out = bool(age_ok)
            if not age_ok:
                if require_fresh_live_brn and "live_brn" not in degraded_sources:
                    degraded_sources.append("live_brn")
            elif valid_live_px and ref_close_t1 > 0.0:
                brn_intra_pct = _clip((live_px / ref_close_t1 - 1.0) * 100.0, -20.0, 20.0)
                if brn_intra_pct <= -2.5:
                    w_crash = _clip((-brn_intra_pct - 2.5) / 2.0, 0.0, 1.0) * 0.55
                    if w_crash > 0.0:
                        display_p = _normalize_probs(
                            [
                                (1.0 - w_crash) * display_p[k] + (w_crash if k == 4 else 0.0)
                                for k in range(len(OIL_REGIMES))
                            ]
                        )
                        down_tbl = V5_REGIME_TABLE["下降"]
                        cluster_eff = min(
                            cluster_eff,
                            (1.0 - w_crash) * cluster_eff + w_crash * float(down_tbl["cluster_unconf"]),
                        )
                        sigma_eff = min(
                            sigma_eff,
                            (1.0 - w_crash) * sigma_eff + w_crash * float(down_tbl["sig_unconf"]),
                        )
                        solo_c_coef = (1.0 - w_crash) * solo_c_coef + w_crash * float(down_tbl["solo_c_coef"])
                        solo_c_cap = (1.0 - w_crash) * solo_c_cap + w_crash * float(down_tbl["solo_c_cap"])
                        if w_crash >= 0.35:
                            effective_regime = "下降"
                            fast_crash_active = True

            # Read-only next-day opening regime preview (zero look-ahead contamination; never writes _base_cache)
            if valid_live_px and len(oil_closes) >= _MIN_OIL_DAYS:
                prev_date_sim = clean_td if clean_td > str(oil_last_date) else f"{oil_last_date}N"
                sim_oil_d = [*oil_dates, prev_date_sim]
                sim_oil_c = [*oil_closes, float(live_px)]
                sim_bundle = dict(macro_bundle)
                sim_bundle["oil_daily_2y"] = (sim_oil_d, sim_oil_c)
                sim_base = self._replay_daily_base(
                    oil_dates=sim_oil_d,
                    oil_closes=sim_oil_c,
                    ho_dates=ho_dates,
                    ho_closes=ho_closes,
                    xop_dates=xop_dates,
                    xop_closes=xop_closes,
                    macro_bundle=sim_bundle,
                )
                sim_oil_ma5 = float(sim_base["oil_ma5"])
                sim_oil_ma15 = float(sim_base["oil_ma15"])
                sim_oil_ratio = (sim_oil_ma5 / sim_oil_ma15) if sim_oil_ma15 > 0.0 else 1.0
                sim_eff_ratio = min(sim_oil_ratio, float(median(crude_ratios))) if crude_ratios else sim_oil_ratio
                g_sim = _clip((sim_eff_ratio - 0.998) / 0.012, 0.0, 1.0)

                sim_policy = self._resolve_policy_from_base(
                    sim_base, g_crude=g_sim, clean_profile=clean_profile
                )
                sim_locked_idx = int(sim_base["locked_idx"])
                sim_locked_regime = OIL_REGIMES[sim_locked_idx]
                sim_probs_out = _format_probs_pct(sim_policy["base_probs"])
                sim_locked_mean = float(sim_probs_out[sim_locked_regime])
                sim_ci_lo_locked = round(
                    _clip(
                        min(float(sim_base["v62_ci_epistemic_lo"][sim_locked_idx]) * 100.0, sim_locked_mean),
                        0.0,
                        100.0,
                    ),
                    1,
                )
                sim_ci_hi_locked = round(
                    _clip(
                        max(float(sim_base["v62_ci_epistemic_hi"][sim_locked_idx]) * 100.0, sim_locked_mean),
                        0.0,
                        100.0,
                    ),
                    1,
                )
                next_day_preview = {
                    "basis_price": round(live_px, 4),
                    "locked_regime": sim_locked_regime,
                    "probs": sim_probs_out,
                    "oil_ma5": round(sim_oil_ma5, 2),
                    "oil_ma15": round(sim_oil_ma15, 2),
                    "ma_gap_pct": (
                        round((sim_oil_ma5 / sim_oil_ma15 - 1.0) * 100.0, 2) if sim_oil_ma15 > 0.0 else 0.0
                    ),
                    "preview_cluster_cap_pct": round(float(sim_policy["cluster_eff"]) * 100.0, 2),
                    "preview_sigma_cap_pct": round(float(sim_policy["sigma_eff"]), 4),
                    "consensus_pct": float(sim_base["v62_consensus_pct"]),
                    "consensus_band": str(sim_base["v62_consensus_band"]),
                    "ci_guard_action": str(sim_policy["ci_guard_action"]),
                    "locked_ci_pct": [sim_ci_lo_locked, sim_ci_hi_locked],
                }

        degraded_mode: str | None = None
        if degraded_sources:
            degraded_mode = "auxiliary_degraded"

        probs_out = _format_probs_pct(display_p)
        ci_by_regime = _build_ci_by_regime(probs_out, base)
        dimension_votes = _build_dimension_votes(base)

        return {
            "enforced": bool(enforced),
            "profile": clean_profile,
            "oil_reference_date": str(base["oil_reference_date"]),
            "oil_close_t1": round(ref_close_t1, 4),
            "locked_regime": locked_regime,
            "effective_regime": effective_regime,
            "probs": probs_out,
            "ma_confirm_ratio": round(g_crude, 4),
            "effective_cluster_cap_pct": round(cluster_eff * 100.0, 2),
            "effective_sigma_cap_pct": round(sigma_eff, 4),
            "solo_crude_coef": round(solo_c_coef, 4),
            "solo_crude_cap": round(solo_c_cap, 4),
            "symbol_multipliers": symbol_multipliers,
            "symbol_tuning_hints": {
                "rev_t_501018": round(float(hints["rev_t_501018"]), 4),
                "unconf_wt": round(float(hints["unconf_wt"]), 4),
                "taper_501018": round(float(hints["taper_501018"]), 4),
            },
            "fast_crash_active": fast_crash_active,
            "live_brn_price": live_brn_price_out,
            "live_brn_chg_pct": live_brn_chg_pct_out,
            "live_brn_fresh": live_brn_fresh_out,
            "next_day_preview": next_day_preview,
            "degraded_mode": degraded_mode,
            "degraded_sources": list(dict.fromkeys(degraded_sources)),
            "consensus_pct": float(base["v62_consensus_pct"]),
            "consensus_band": str(base["v62_consensus_band"]),
            "ci_guard_action": ci_guard_action,
            "active_dimensions": list(base["active_dimensions"]),
            "missing_dimensions": list(base["missing_dimensions"]),
            "ci_by_regime": ci_by_regime,
            "dimension_votes": dimension_votes,
        }
