/* ============================================================
   CleanPlate —— 一次跑完所有验收
   ============================================================

   四套检查各自独立、都能单跑：
     · scripts/selftest.py     后端引擎（34 项）—— 选区外 bit 级不变等硬约束
     · scripts/ui_probe.js     界面主流程（30 项）—— 涂抹/修复/撤销/保存
     · scripts/select_probe.js 智能选区（23 项）—— 套索/框选/魔棒
     · scripts/pick_probe.js   选片窗口（24 项）—— 缩略图网格/大图预览/键盘导航

   但每次改完代码挨个手动跑容易漏。这个脚本把它们串起来，
   一次给出总账，任何一套挂了都会以非 0 退出码结束，
   方便挂到 pre-push 钩子或 CI 上。

   注意：后三套都需要服务已经在 http://127.0.0.1:8240 上跑着。

   用法：
     node scripts/run_all_checks.js
     node scripts/run_all_checks.js --only=select
   ============================================================ */
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');

// 优先用项目 venv 里的 python；没有就退到系统 python
function findPython() {
  const cands = [
    path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(ROOT, '.venv', 'bin', 'python'),
  ];
  for (const c of cands) if (fs.existsSync(c)) return c;
  return 'python';
}

const SUITES = [
  {
    key: 'selftest',
    name: '后端引擎自检',
    cmd: findPython(),
    args: ['scripts/selftest.py'],
  },
  {
    key: 'ui',
    name: '界面主流程',
    cmd: process.execPath,
    args: ['scripts/ui_probe.js'],
  },
  {
    key: 'select',
    name: '智能选区',
    cmd: process.execPath,
    args: ['scripts/select_probe.js'],
  },
  {
    key: 'pick',
    name: '选片窗口',
    cmd: process.execPath,
    args: ['scripts/pick_probe.js'],
  },
];

const onlyArg = process.argv.find((a) => a.startsWith('--only='));
const only = onlyArg ? onlyArg.split('=')[1].split(',') : null;
const list = only ? SUITES.filter((s) => only.includes(s.key)) : SUITES;

if (!list.length) {
  console.error(`没有匹配的检查项。可选：${SUITES.map((s) => s.key).join(', ')}`);
  process.exit(1);
}

console.log('');
console.log('╔══════════════════════════════════════════════════╗');
console.log('║   CleanPlate 全套验收                            ║');
console.log('╚══════════════════════════════════════════════════╝');

const results = [];

for (const s of list) {
  console.log('');
  console.log(`▶ ${s.name}   (${s.cmd} ${s.args.join(' ')})`);
  console.log('─'.repeat(58));

  // stdio: inherit —— 让子进程的彩色输出直接透出来，不要在这层丢信息
  const r = spawnSync(s.cmd, s.args, { cwd: ROOT, stdio: 'inherit' });
  const code = r.status === null ? 1 : r.status;
  results.push({ ...s, code });
}

console.log('');
console.log('╔══════════════════════════════════════════════════╗');
console.log('║   总账                                           ║');
console.log('╚══════════════════════════════════════════════════╝');
for (const r of results) {
  console.log(`  ${r.code === 0 ? '✓ 通过' : '✗ 失败'}   ${r.name}`);
}

const failed = results.filter((r) => r.code !== 0);
console.log('');
if (failed.length) {
  console.log(`  ${failed.length} / ${results.length} 套失败`);
  process.exit(1);
}
console.log(`  全部 ${results.length} 套通过`);
console.log('');
