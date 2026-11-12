"""Read-only discovery and explicit selection of project verification entry points."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

KINDS = {'code', 'documentation', 'service', 'other'}


def validate_commands(checks):
    """Reject pasted numbered instructions before approving them as shell checks."""
    for number, command in enumerate(checks, 1):
        if re.match(r'^\s*\d+(?:[、．]|\.\s)', command):
            raise ValueError(f'第 {number} 条验收命令含文字编号，请填写实际可执行命令；'
                             '测试环境和验收范围说明请填写到需求或补充要求中。')


def coverage_warning(checks):
    if not checks or all(re.fullmatch(r'\s*git\s+diff\s+--check\s*', c) for c in checks):
        return '当前验收仅包含格式检查或没有执行检查，不能证明功能可用。请补充与本轮需求对应的行为测试；reviewer 仍需核对未覆盖能力。'
    return ''


def read(path, root):
    if not path.is_file() or not path.resolve().is_relative_to(root) or path.stat().st_size > 512 * 1024:
        return ''
    return path.read_text(encoding='utf-8', errors='replace')


def scan(cwd, kind='code', python=''):
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir() or not isinstance(kind,str) or kind not in KINDS:
        raise ValueError('请选择存在的项目目录和有效任务类型')
    if not isinstance(python, str) or len(python) > 1000 or '\0' in python:
        raise ValueError('Python 解释器格式无效')
    interpreter = python.strip() or str(root / '.venv/bin/python' if (root / '.venv/bin/python').exists() else Path(sys.executable))
    if not Path(interpreter).is_absolute():
        raise ValueError('请填写 Python 解释器的绝对路径')
    checks, sources = [], {}
    def source(name):
        content = read(root / name, root)
        if content:
            sources[name] = hashlib.sha256(content.encode()).hexdigest()
        return content
    def add(ident, title, purpose, argv, *, dependencies=(), artifacts=(), effects='', estimate='依项目规模而定', recommended=False):
        checks.append({'id': ident, 'title': title, 'purpose': purpose, 'command': shlex.join(argv),
                       'executable': argv[0], 'dependencies': list(dependencies), 'artifacts': list(artifacts),
                       'effects': effects or '执行项目中的代码，可能生成缓存或测试输出；扫描不会执行。',
                       'estimate': estimate, 'recommended': recommended})
    # Bounded file inspection; never execute discovery hooks or import project code.
    test_paths, node_paths, swift_packages = [], [], []
    visited = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        visited += 1
        if visited > 500:
            break
        relative = Path(directory).relative_to(root)
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in {'node_modules','build','dist','target','site-packages','__pycache__'}) if len(relative.parts) < 4 else []
        for name in sorted(files):
            path = Path(directory) / name
            if name == 'Package.swift' and len(swift_packages) < 20:
                manifest = str(path.relative_to(root))
                if source(manifest):
                    swift_packages.append(manifest)
            if re.match(r'(test_.*|.*_test)\.py$', name) and len(test_paths) < 80:
                test_paths.append(str(path.relative_to(root)))
            if (re.fullmatch(r'(?:test(?:[_.-].*)?|.*[_.-]test)\.(?:mjs|cjs|js)', name)
                    and len(node_paths) < 80 and path.is_file()
                    and path.resolve().is_relative_to(root) and path.stat().st_size <= 512 * 1024):
                node_paths.append(str(path.relative_to(root)))
        if len(test_paths) >= 80 and len(node_paths) >= 80:
            break
    test_text = '\n'.join(source(p) for p in test_paths)
    config = '\n'.join(source(p) for p in ('pyproject.toml','pytest.ini','setup.cfg','requirements.txt','requirements-dev.txt'))
    if test_paths:
        if any(p.startswith('tests/') and 'unittest' in read(root/p,root) for p in test_paths):
            # Empty test discovery is not proposed unless real tests were found.
            add('python-unittest', 'Python 自动化测试', '验证现有测试中的成功、失败及回归断言',
                [interpreter, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], artifacts=test_paths,
                dependencies=(), recommended=True)
        if 'pytest' in config or 'pytest' in test_text or re.search(r'^def test_', test_text, re.M):
            add('python-pytest', 'Python pytest 测试', '验证项目现有 pytest 测试', [interpreter, '-m', 'pytest', '-q'],
                dependencies=['pytest'], artifacts=test_paths, recommended=not any(c['id']=='python-unittest' for c in checks))
    if node_paths:
        for name in node_paths:
            sources[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        add('node-test', 'Node 内置自动化测试', '使用 Node 内置测试运行器验证已发现测试的成功、失败及回归断言',
            ['node', '--test', *node_paths], artifacts=node_paths,
            effects='执行列出的项目测试代码，可能写入文件、启动子进程或访问网络；不运行 npm 生命周期，不安装依赖。扫描只读取文件。',
            recommended=True)
    ndr = all(source(p) for p in ('scripts/check_docs.py','scripts/check_hzd_contracts.py','scripts/check_development_contracts.py',
                                  'scripts/review_development.py','docs/sql/validation/2026-11-05-code-review.json'))
    for filename, title, purpose, deps in [
            ('check_docs.py','文档结构与链接','检查文档登记、链接与引用',[]),
            ('check_hzd_contracts.py','接口契约一致性','检查 HZD OpenAPI 契约与样例',['yaml','jsonschema']),
            ('check_development_contracts.py','需求与开发契约','检查需求、资源及命令清单的登记一致性',[])]:
        name = 'scripts/' + filename
        if source(name):
            add(filename, title, purpose, [interpreter, name], dependencies=deps, artifacts=[name],
                effects='执行已有检查脚本；不自动运行数据库或部署。脚本行为应结合项目说明确认。',
                estimate='通常数秒至数分钟', recommended=kind=='documentation')
    if ndr:
        from auto_moniter import documentation
        helper = Path(documentation.__file__).resolve()
        # The baseline is created outside the target only after an explicit task
        # selection, never while scanning. Filled with the actual run path later.
        add('ndr-documentation', '当前文档的 100 个审阅焦点', '保留历史运行证据，核验当前静态文档；不代表生产验收',
            [interpreter, str(helper), '--project', str(root), '--baseline', '{run_dir}/documentation-baseline.json.gz'],
            dependencies=['yaml','jsonschema'], artifacts=['scripts/review_development.py'],
            effects='只读检查；旧文档哈希差异明确归为历史证据，不改历史记录。创建任务时保存文件哈希基线。',
            recommended=kind=='documentation')
    package = source('package.json')
    if package:
        try:
            data = json.loads(package)
        except ValueError:
            data = {}
        if not isinstance(data,dict):
            data = {}
        scripts = data.get('scripts', {})
        if isinstance(scripts, dict):
            for name, title in [('test','JavaScript 自动化测试'),('lint','代码规范检查'),('typecheck','类型检查'),('build','构建检查')]:
                if isinstance(scripts.get(name), str) and scripts[name].strip():
                    add('npm-' + name, title, '执行 package.json 已有 ' + name + ' 脚本', ['npm','run',name],
                        artifacts=['package.json'], effects='可能执行 pre/post 生命周期脚本、写入构建产物或访问网络；不会自动安装依赖。',
                        recommended=False)
    if source('go.mod'):
        add('go-test', 'Go 自动化测试', '编译并执行项目 Go 测试', ['go','test','./...'], artifacts=['go.mod'],
            effects='可能下载模块、写入构建缓存或执行集成测试。', estimate='数分钟或更久')
    if source('Cargo.toml'):
        add('cargo-test', 'Rust 自动化测试', '编译并执行项目 Rust 测试', ['cargo','test','--workspace'], artifacts=['Cargo.toml'],
            effects='可能下载依赖、执行 build.rs 并生成 target 目录。', estimate='数分钟或更久')
    for index, manifest in enumerate(swift_packages, 1):
        package_dir = str(Path(manifest).parent)
        add('swift-test-' + str(index), 'Swift 包测试：' + package_dir,
            '编译并执行该 Swift Package 声明的测试；不等同于 Apple 应用完整验收',
            ['swift', 'test', '--package-path', package_dir], artifacts=[manifest],
            effects='可能解析包清单、下载依赖和生成 .build；Apple 框架需要对应 SDK。扫描不执行 Swift 或安装工具链。',
            estimate='数分钟或更久')
    ci = []
    workflows = root / '.github/workflows'
    if workflows.is_dir() and workflows.resolve().is_relative_to(root):
        for p in sorted(workflows.iterdir())[:20]:
            if p.suffix in ('.yml','.yaml') and source(str(p.relative_to(root))):
                ci.append(str(p.relative_to(root)))
    if (root / '.git').exists():
        add('git-diff', '改动格式检查', '检查冲突标记及空白错误；不验证业务功能', ['git','diff','--check'],
            effects='只读本地 Git 改动。', estimate='通常数秒', recommended=True)
    signature = hashlib.sha256(json.dumps({'sources':sources,'python':interpreter,'kind':kind,'checks':checks},sort_keys=True).encode()).hexdigest()
    return {'cwd':str(root),'task_type':kind,'python':interpreter,'checks':checks,'signature':signature,'ci_sources':ci,
            'semantic_review':{'title':'独立语义审核','purpose':'reviewer 核对需求完整性、描述准确性与未覆盖边界；不能代替运行测试。'},
            'unverified':['未声明或未选择的功能、数据库/HTTP/端到端、性能和客户验收均不能由扫描结果推定通过。'],
            'notice':'扫描只读取有限项目文件，不执行测试、不安装依赖。没有发现入口不代表项目没有验证能力。格式检查或旧测试通过不证明新增功能可用，请选择需求对应的行为验证。'}


def select(cwd, plan):
    if not isinstance(plan, dict):
        raise ValueError('验收方案格式无效')
    criteria = plan.get('criteria', '')
    ids = plan.get('selected_ids', [])
    if not isinstance(criteria, str) or not criteria.strip() or len(criteria) > 8000 or '\0' in criteria:
        raise ValueError('请用文字填写 1 至 8000 字的完成标准')
    if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):
        raise ValueError('检查项选择无效')
    found = scan(cwd, plan.get('task_type','code'), plan.get('python',''))
    if plan.get('signature') != found['signature']:
        raise ValueError('项目检查入口或解释器已变化，请重新扫描并确认检查项')
    by_id = {c['id']:c for c in found['checks']}
    if set(ids) - by_id.keys():
        raise ValueError('所选检查入口不存在，请重新扫描')
    if 'node-test' in ids:
        # Watcher checks run through a login shell, whose PATH may differ from
        # the console's. Bind the selected runner to the binary we preflight.
        node = shutil.which('node')
        if node:
            node = os.path.abspath(node)
            check = by_id['node-test']
            check['executable'] = node
            check['command'] = shlex.join([node, *shlex.split(check['command'])[1:]])
    return {**found,'criteria':criteria.strip(),'selected_ids':ids,'selected':[by_id[i] for i in ids]}


def preflight(plan):
    results=[]
    python_probe = None
    dependencies = sorted({m for c in plan['selected'] if c['executable']==plan['python'] for m in c['dependencies']})
    for check in plan['selected']:
        errors=[]
        executable=check['executable']
        if not shutil.which(executable):
            errors.append('找不到可执行程序：' + executable)
        if check['id'].startswith('python-') or executable==plan['python']:
            if not errors:
                code='import sys,importlib.util,json;print(json.dumps({"version":list(sys.version_info[:2]),"missing":[m for m in '+repr(dependencies)+' if importlib.util.find_spec(m) is None]}))'
                try:
                    if python_probe is None:
                        python_probe=subprocess.run([executable,'-I','-c',code],cwd='/tmp',capture_output=True,text=True,timeout=5)
                    result=python_probe
                    data=json.loads(result.stdout) if result.returncode==0 else {}
                    if not isinstance(data,dict) or not isinstance(data.get('version'),list) or len(data['version'])!=2 or any(type(v) is not int for v in data['version']):
                        errors.append('解释器无法完成环境检查')
                        data = {}
                    elif data['version'] < [3,11]:errors.append('需要 Python 3.11 或更高版本')
                    missing = [m for m in data.get('missing',[]) if m in check['dependencies']]
                    if missing:errors.append('缺少依赖：'+', '.join(missing))
                except (OSError,ValueError,subprocess.TimeoutExpired):errors.append('解释器检查超时或输出无效')
        if check['id'].startswith('npm-'):
            package=json.loads((Path(plan['cwd'])/'package.json').read_text())
            if (package.get('dependencies') or package.get('devDependencies')) and not (Path(plan['cwd'])/'node_modules').is_dir():
                errors.append('项目声明了依赖但没有 node_modules；请先准备环境，不会自动安装')
        results.append({'id':check['id'],'title':check['title'],'status':'environment_unavailable' if errors else 'ready',
                        'errors':errors,'note':'仅检查运行前提，尚未执行功能检查。'})
    return {'ready':all(r['status']=='ready' for r in results),'checks':results,'executed':False,
            'coverage_warning':coverage_warning([c['command'] for c in plan['selected']])}
