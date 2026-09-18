// 临时校验脚本：提取 knowledge_graph 生成的 HTML 内联 JS 并做语法检查
const fs = require("fs");
const path = require("path");
const file = process.argv[2];
const html = fs.readFileSync(file, "utf-8");
const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
let m, i = 0, fail = 0;
while ((m = re.exec(html)) !== null) {
    i++;
    try {
        new Function(m[1]);
        console.log(`script#${i}: OK (${m[1].length} chars)`);
    } catch (e) {
        fail++;
        console.log(`script#${i}: SYNTAX ERROR -> ${e.message}`);
    }
}
console.log(fail === 0 && i > 0 ? `ALL ${i} INLINE SCRIPTS PASS` : `FAILED (${fail}/${i})`);
process.exit(fail === 0 ? 0 : 1);
