import fs from 'node:fs/promises';
import path from 'node:path';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const root = 'E:/хакатон/Hacatone';
const out = path.join(root, 'outputs/economics-20261128');
const input = path.join(root, 'ИИкузница — калькулятор экономики решения.xlsx');
const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const sheet = wb.worksheets.getItem('Расчёт');
const preview = await wb.render({sheetName:'Расчёт', range:'A1:J19', scale:1.5, format:'png'});
await fs.writeFile(path.join(out,'before.png'), new Uint8Array(await preview.arrayBuffer()));
console.log(JSON.stringify({formulas:sheet.getRange('I8:I13').formulas,values:sheet.getRange('A9:J12').values}));
if (!process.argv.includes('--fill')) process.exit(0);

const groups = new Map();
let cases = 0;
const directory = path.join(root,'corpus/live-semantics-p0-reviewed-full');
for (const name of await fs.readdir(directory)) {
  if (!name.endsWith('.json')) continue;
  const record = JSON.parse(await fs.readFile(path.join(directory,name),'utf8'));
  if (!record.expected.manual) continue;
  cases++;
  for (const request of record.llm_requests) {
    if (request.status !== 200) throw new Error('Unexpected non-successful API response');
    const generated = JSON.parse(request.generated_json || '{}');
    const key = generated.verdicts ? 'verification' : generated.tool === 'search' ? 'search' : 'generation';
    const stats = groups.get(key) || {calls:0,input:0,output:0};
    stats.calls++;
    stats.input += request.usage.prompt_tokens;
    stats.output += request.usage.completion_tokens;
    groups.set(key,stats);
  }
}
if (cases !== 16) throw new Error('Expected the full set of 16 technical questions');
const rows = [ ['search','Поиск: выбор запроса'], ['generation','Формирование ответа'],
               ['verification','Проверка оснований'] ];
sheet.getRange('I3').values = [[82.313]];
for (let i=0;i<rows.length;i++) {
  const [key,label] = rows[i];
  const g=groups.get(key);
  sheet.getRange(`A${9+i}:H${9+i}`).values = [[label,'gpt-6-luna',g.calls/cases,
    g.input/g.calls,g.output/g.calls,0.1,0.5,'$']];
}
sheet.getRange('A12:H12').values = [['Локальный поиск / ML','Без платного API',0,0,0,0,0,'₽']];
sheet.getRange('C9:E11').setNumberFormat('0.00');
sheet.getRange('I9:I13').setNumberFormat('0.0000');
sheet.getRange('I3').setNumberFormat('0.0000');
sheet.getRange('A17').values = [[
  'Цифровой механик. 1 запрос = технический вопрос; среднее 16 случаев, 56 вызовов, повторы включены. Замер 27.11.2026; корпус/live-semantics-p0-reviewed-full.'
]];
sheet.getRange('A18').values = [[
  'Тариф Standard без скидки кеша: developers.openai.com/api/docs/models/gpt-6-luna. Курс ЦБ 26.11.2026: cbr.ru/currency_base/daily/?UniDbQuery.To=26.11.2026'
]];
sheet.getRange('A19').values = [[
  'Расчёт API по публичному тарифу. C — среднее число вызовов, D/E — на вызов. Локальный ПК; электричество и труд не оценены. Иные тарифы, кеш и аренда сервера изменят итог.'
]];
sheet.getRange('A17:J19').format.wrapText = true;
sheet.getRange('A17:J19').format.rowHeight = 34;
await wb.recalculate();
const totals = sheet.getRange('I9:I13').values;
const expected=[...groups.values()].reduce((sum,g)=>sum+(g.input*0.1+g.output*0.5)/1e6/cases*82.313,0);
console.log(JSON.stringify({cases,groups:Object.fromEntries(groups),totals,expected}));
if (Math.abs(Number(totals[4][0])-expected)>1e-8) throw new Error('Formula result differs from token calculation');
const result = await SpreadsheetFile.exportXlsx(wb);
await result.save(path.join(out,'Цифровой механик — экономика решения.xlsx'));
const after = await wb.render({sheetName:'Расчёт',range:'A1:J19',scale:1.5,format:'png'});
await fs.writeFile(path.join(out,'after.png'),new Uint8Array(await after.arrayBuffer()));
