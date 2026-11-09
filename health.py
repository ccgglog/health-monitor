# -*- coding: utf-8 -*-
"""health.py — 服务健康分级判定（零依赖，可直接运行）

核心观点：「进程在跑」和「服务正常」是两件事。
  端口在监听 → 可能是代理在替死上游
  进程存在   → 可能加载的是旧代码
  日志在更新 → 可能是别的模块在写

所以要三个维度交叉：文件时间戳 + 心跳 + 面板进程。

输出不是 pass/fail，而是带处置建议的分级：
  OK            正常
  LATE          略超上限，可能是这一轮还没跑 —— 不用管
  MISSED        超时太久，判定漏跑 —— 要补跑
  STALLED       有驱动但长时间没产出 —— 要查
  PANEL_DOWN    面板进程不在 —— 要重启
  GUARD_GAP     守护线程近期有空档 —— 要查面板
  停用          配置里 enabled=false —— 不参与判定，不报警

为什么要分级：LATE 不用管、MISSED 要补跑、PANEL_DOWN 要重启，
一个布尔值给不出处置方案。

用法：
  python health.py            # 内置合成快照演示（退出码恒 0）
  python health.py --json     # 机器可读
  python health.py --strict   # 退出码反映判定（0 正常 / 1 警告 / 2 需处理）
"""
import json
import sys

# 阈值（每条都来自真实事故时长）
WARN_H = 26.0          # 略超正常上限：这一轮可能还没跑
FAIL_H = 30.0          # 漏跑一轮（历史上有过 39h、62.5h 两次）
GUARD_FAIL_H = 2.0     # 守护任务 30 秒一跳，2 小时没动 = 面板有问题
HB_GAP_WARN_H = 2.0    # 近 24h 内最大空档超过它 = 守护曾经停过
SPREAD_WARN_H = 6.0    # 各任务 age 极差超过它 = 有任务被漏掉


def judge_task(age_h, enabled=True, driver='cron'):
    """单个任务的判定。返回 (level, code, note)"""
    if not enabled:
        return ('SKIP', 'DISABLED', '配置已停用，不参与判定')
    if age_h >= FAIL_H:
        return ('FAIL', 'MISSED', '超过 %.0fh，判定漏跑，需要补跑' % FAIL_H)
    if age_h >= WARN_H:
        return ('WARN', 'LATE', '超过 %.0fh，这一轮可能还没跑' % WARN_H)
    if driver == 'daemon' and age_h >= GUARD_FAIL_H:
        return ('WARN', 'STALLED',
                '常驻进程应每 30s 一跳，已 %.1fh 未动' % age_h)
    return ('OK', 'OK', '')


def judge_panel(alive, single_instance=True):
    if not alive:
        return ('FAIL', 'PANEL_DOWN', '面板进程不存在，需要重启')
    if not single_instance:
        return ('FAIL', 'DUP_PANEL', '出现多个实例，会互相覆盖状态')
    return ('OK', 'OK', '')


def judge_heartbeat(gap_h, window_h=24.0):
    if gap_h >= HB_GAP_WARN_H:
        return ('WARN', 'GUARD_GAP',
                '近 %.0fh 内最大空档 %.1fh，守护曾经停过' % (window_h, gap_h))
    return ('OK', 'OK', '')


def verdict(snapshot):
    """总判定。snapshot = {tasks, panel, heartbeat}"""
    items = []

    for t in snapshot['tasks']:
        level, code, note = judge_task(t['age_h'], t.get('enabled', True),
                                       t.get('driver', 'cron'))
        items.append({'name': t['name'], 'level': level, 'code': code,
                      'age_h': t['age_h'], 'note': note})

    level, code, note = judge_panel(snapshot['panel']['alive'],
                                    snapshot['panel'].get('single', True))
    items.append({'name': '面板', 'level': level, 'code': code,
                  'age_h': 0.0, 'note': note})

    level, code, note = judge_heartbeat(snapshot['heartbeat']['gap_h'])
    items.append({'name': '守护心跳', 'level': level, 'code': code,
                  'age_h': snapshot['heartbeat']['gap_h'], 'note': note})

    # 跨任务一致性：age 极差过大 = 有任务被漏掉
    act = [i for i in items if i['level'] != 'SKIP' and i['name'] != '面板']
    if len(act) >= 2:
        spread = max(i['age_h'] for i in act) - min(i['age_h'] for i in act)
        if spread >= SPREAD_WARN_H:
            items.append({'name': '跨任务一致性', 'level': 'WARN',
                          'code': 'SPREAD',
                          'age_h': round(spread, 1),
                          'note': '各任务 age 极差 %.1fh，有任务可能被漏掉'
                                  % spread})

    levels = [i['level'] for i in items]
    if 'FAIL' in levels:
        overall, code_out = 2, 'FAIL'
    elif 'WARN' in levels:
        overall, code_out = 1, 'WARN'
    else:
        overall, code_out = 0, 'OK'

    return {'items': items, 'overall': overall, 'verdict': code_out}


# ---------------------------------------------------------------- 演示快照
# 场景：一个每天定时跑的数据平台（ETL + 日报 + 行情同步）。
# 任务是通用名字，只演示判定逻辑，不对应任何真实项目。
def build_demo():
    return {
        'tasks': [
            {'name': 'orders_etl',  'age_h': 2.6,  'enabled': True,  'driver': 'cron'},
            {'name': 'users_etl',   'age_h': 2.6,  'enabled': True,  'driver': 'cron'},
            {'name': 'report_daily','age_h': 2.6,  'enabled': True,  'driver': 'cron'},
            {'name': 'sync_prices', 'age_h': 0.0,  'enabled': True,  'driver': 'daemon'},
            # 已停用的任务：不该报警，也不该参与一致性判定
            {'name': 'legacy_a',    'age_h': 651.6, 'enabled': False, 'driver': 'daemon'},
            {'name': 'legacy_b',    'age_h': 1112.9,'enabled': False, 'driver': 'daemon'},
            # 漏跑
            {'name': 'backlog_etl', 'age_h': 39.6, 'enabled': True,  'driver': 'cron'},
        ],
        'panel': {'alive': True, 'single': True},
        'heartbeat': {'gap_h': 0.02},
    }


def main():
    snap = build_demo()
    v = verdict(snap)
    strict = '--strict' in sys.argv

    if '--json' in sys.argv:
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return v['overall'] if strict else 0

    print('=' * 66)
    print('服务健康检查（演示快照）')
    print('=' * 66)
    print('%-14s %-8s %-8s %10s  %s' % ('任务', '等级', '代码', '年龄(h)', '说明'))
    print('-' * 66)
    for i in v['items']:
        if i['level'] == 'SKIP':
            print('%-14s %-8s %-8s %10.1f  %s'
                  % (i['name'], '停用', i['code'], i['age_h'], i['note']))
            continue
        print('%-14s %-8s %-8s %10.1f  %s'
              % (i['name'], i['level'], i['code'], i['age_h'], i['note']))
    print('-' * 66)
    print('判定结果: %s（对应退出码 %d）' % (v['verdict'], v['overall']))
    print('=' * 66)
    print('注1：legacy_a / legacy_b 已停用，不报警也不参与跨任务一致性判定。')
    print('     如果不排除它们，age 极差会把整个池子判成异常。')
    print('注2：演示模式退出码恒为 0，方便直接 python health.py 看效果。')
    print('     接实际监控数据时加 --strict，退出码才反映结论（0/1/2），')
    print('     这样能直接塞进 cron：python health.py --strict || 告警')
    return 0 if not strict else v['overall']


if __name__ == '__main__':
    sys.exit(main())
