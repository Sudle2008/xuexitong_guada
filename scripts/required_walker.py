# -*- coding: utf-8 -*-
"""
必修部分顺序学习遍历器 v4（无头 Chrome, 人机协同答题）
流程/叶子: 跳过已完成视频 -> 播放任务视频 -> 测验: 出题等待人工作答 -> 提交并验证 -> 下一节
- 视频完成判定: job=True 须 isPassed=True; job=None+isPassed=True 已完成; job=None+isPassed=False 非任务点(看过即可)
- 测验 iframe 加载: about:blank 隔离 + 最多3次重试
- 提交验证: 拦截 addStudentWorkNewWeb 响应 JSON
文件: pending_quiz.json(题目) / quiz_answers.json(答案 {qid: [labels]})
"""
import json, re, time, argparse, base64, os, sys
import importlib.util
from pathlib import Path
from playwright.sync_api import sync_playwright

# 运行上下文全部由外部注入(--workdir / 环境变量 XXW_WORKDIR), 引擎本身不含任何账号/课程/路径信息
BASE = None            # 必须由 --workdir 或 XXW_WORKDIR 指定, init_course_context 赋值
COURSE_ID = None       # 以下由 <workdir>/course_config.json 填充
CLAZZ_ID = None
CPI = 0
DOMAIN = ''            # 平台域名(必填)
COOKIE_DOMAINS = []    # Cookie 生效域(可选, 默认 ['.chaoxing.com'])
CHROME = os.environ.get('XXW_CHROME', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
MAPPING = {}           # 可选 <workdir>/font_map.json(码点十进制 -> 汉字)
# 以下路径在 init_course_context 内按 BASE 赋值
STATE = LOG = PENDING = ANSWERS = None

def decode(s):
    return ''.join(MAPPING.get(ord(c), c) for c in s)

def init_course_context(base):
    """从工作区加载课程上下文(course_config.json), 兼容旧默认目录。"""
    global COURSE_ID, CLAZZ_ID, CPI, BASE, DOMAIN, COOKIE_DOMAINS, MAPPING
    global STATE, LOG, PENDING, ANSWERS
    BASE = Path(base)
    conf_path = BASE / 'course_config.json'
    if not conf_path.exists():
        raise SystemExit('缺少配置: %s (需提供 courseId/clazzId/domain)' % conf_path)
    conf = json.loads(conf_path.read_text(encoding='utf-8'))
    COURSE_ID = conf.get('courseId')
    CLAZZ_ID = conf.get('clazzId')
    CPI = conf.get('cpi', 0)
    DOMAIN = (conf.get('domain') or '').strip()
    if not (COURSE_ID and CLAZZ_ID and DOMAIN):
        raise SystemExit('course_config.json 需包含 courseId/clazzId/domain')
    cdomains = conf.get('cookie_domains') or ['.chaoxing.com']
    COOKIE_DOMAINS = cdomains if isinstance(cdomains, list) else [str(cdomains)]
    fm = BASE / 'font_map.json'
    if fm.exists():
        MAPPING = {int(k): v for k, v in json.loads(fm.read_text(encoding='utf-8')).items()}
    STATE = BASE / 'required_state.json'
    LOG = BASE / 'required_watch.log'
    PENDING = BASE / 'pending_quiz.json'
    ANSWERS = BASE / 'quiz_answers.json'

def log(msg):
    line = '[%s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')

def load_cookies(ctx):
    txt = (BASE / 'cookies.txt').read_text(encoding='utf-8')
    d = {}
    for part in re.split(r'[;\n]', txt):
        p = part.strip()
        if not p or '=' not in p:
            continue
        n, _, v = p.partition('=')
        d[n.strip()] = v.strip()
    cookies = []
    for n, v in d.items():
        for dm in COOKIE_DOMAINS:
            cookies.append({'name': n, 'value': v, 'domain': dm, 'path': '/'})
    ctx.add_cookies(cookies)

def rq_session():
    import requests
    text = (BASE / 'cookies.txt').read_text(encoding='utf-8')
    cookies = {}
    for part in re.split(r'[;\n]', text):
        p = part.strip()
        if not p or '=' not in p:
            continue
        n, _, v = p.partition('=')
        cookies[n.strip()] = v.strip()
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                      'Referer': 'https://%s/mycourse/studentstudy?courseId=%d&clazzid=%d' % (DOMAIN, COURSE_ID, CLAZZ_ID)})
    for n, v in cookies.items():
        for dm in COOKIE_DOMAINS:
            s.cookies.set(n, v, domain=dm)
    return s

def cards_page(s, kid, num):
    r = s.get('https://%s/mooc-ans/knowledge/cards' % DOMAIN,
              params={'clazzid': CLAZZ_ID, 'courseid': COURSE_ID, 'knowledgeid': kid,
                      'num': num, 'ut': 's', 'cpi': '', 'v': '2025-0424-1038-3',
                      'mooc2': 1, 'isMicroCourse': '', 'editorPreview': ''}, timeout=25)
    return r.text if r.status_code == 200 else ''

def parse_marg(html):
    ms = list(re.finditer(r'mArg\s*=\s*\{', html))
    if not ms:
        return None
    st = ms[-1].end() - 1
    d = 0
    en = -1
    for i in range(st, len(html)):
        if html[i] == '{':
            d += 1
        elif html[i] == '}':
            d -= 1
            if d == 0:
                en = i + 1
                break
    if en < 0:
        return None
    try:
        return json.loads(html[st:en])
    except Exception:
        return None

def video_state(html):
    """返回 (objectId, duration, job, isPassed) 或 None"""
    marg = parse_marg(html)
    if not marg:
        return None
    for a in marg.get('attachments', []):
        if a.get('objectId') and (a.get('type') == 'video' or a.get('property', {}).get('module') == 'insertvideo'):
            return (a.get('objectId'), a.get('attDuration', 0), a.get('job'), a.get('isPassed', False))
    return None

def video_states(html):
    """返回该卡内全部视频附件 (objectId, duration, job, isPassed) 列表。
    一张卡片可能内嵌多个 insertvideo 视频, 只返回第一个会漏放。"""
    marg = parse_marg(html)
    if not marg:
        return []
    out = []
    for a in marg.get('attachments', []):
        if a.get('objectId') and (a.get('type') == 'video' or a.get('property', {}).get('module') == 'insertvideo'):
            out.append((a.get('objectId'), a.get('attDuration', 0), a.get('job'), a.get('isPassed', False)))
    return out

def finish_doc_job(s, kid, att):
    """完成「文档」任务点(insertdoc)。平台接口: /mooc-ans/job/document
    返回 status=true 即该任务点完成(卡片页 job 会变为 None)。"""
    prop = att.get('property') or {}
    jobid = att.get('jobid') or prop.get('jobid')
    if not jobid:
        return False
    params = {'jobid': jobid, 'knowledgeid': kid, 'courseid': COURSE_ID, 'clazzid': CLAZZ_ID,
              'jtoken': att.get('jtoken', ''), 'checkMicroTopic': 'true',
              'microTopicId': att.get('microTopicId', ''), 'courseEngineInfo': 'false'}
    try:
        r = s.get('https://%s/mooc-ans/job/document' % DOMAIN, params=params, timeout=30)
        j = r.json()
        log('  -> 文档任务点响应: %s' % str(j)[:120])
        return bool(j.get('status'))
    except Exception as e:
        log('  ! 文档任务点提交异常: %s' % str(e)[:60])
        return False

def doc_jobs(html):
    """抽出该卡内未完成的文档任务点附件列表。"""
    marg = parse_marg(html)
    if not marg:
        return []
    out = []
    for a in marg.get('attachments', []):
        if (a.get('property') or {}).get('module') == 'insertdoc' and a.get('job'):
            out.append(a)
    return out

def recheck_prev(s, prev_kid, prev_name):
    """重载上一节卡片页, 核对视频/测验通过状态(用于「通过但未解锁」诊断)。
    返回视频任务点是否全部通过。"""
    ph = cards_page(s, prev_kid, 0)
    marg = parse_marg(ph)
    vids = []
    if marg:
        for a in marg.get('attachments', []):
            if a.get('objectId') and (a.get('type') == 'video' or a.get('property', {}).get('module') == 'insertvideo'):
                vids.append((a.get('property', {}).get('title', a.get('name', '')),
                             a.get('attDuration', 0), a.get('job'), a.get('isPassed')))
    log('  ... 上一节 [%s] 视频任务点 %d 个:' % (prev_name, len(vids)))
    allpass = True
    for t, dur, job, passed in vids:
        log('    - %-26s dur=%s job=%s isPassed=%s' % (str(t)[:26], dur, job, passed))
        if job is True and not passed:
            allpass = False
    try:
        ph1 = cards_page(s, prev_kid, 1)
        is_work = 'module="work"' in ph1
        redo = ('重做' in ph1) or ('未达到及格线' in ph1)
        log('  ... 上一节测验卡: len=%d work=%s 重做/未达线标记=%s' % (len(ph1), is_work, redo))
    except Exception as e:
        log('  ... 测验卡核对异常: %s' % str(e)[:60])
    log('  ... 上一节视频任务点全部通过: %s' % allpass)
    return allpass

def find_video_frame(page, timeout_ms=15000):
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                if fr.evaluate("() => !!document.querySelector('video')"):
                    return fr
            except Exception:
                continue
        time.sleep(0.5)
    return None

def find_video_frame_at(page, idx, timeout_ms=15000):
    """返回第 idx 个(0-based)含 <video> 的子 frame。用于单卡多视频场景。"""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        found = 0
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                if fr.evaluate("() => !!document.querySelector('video')"):
                    if found == idx:
                        return fr
                    found += 1
            except Exception:
                continue
        time.sleep(0.5)
    return None

def play_video(page, rate, fallback_dur=0):
    fr = find_video_frame(page)
    return play_video_frame(page, fr, rate, fallback_dur=fallback_dur)

def play_video_frame(page, fr, rate, fallback_dur=0):
    """播放指定 frame 内的 <video> 直到结束。fr=None 时返回 novideoframe。"""
    if not fr:
        return {'ok': False, 'reason': 'novideoframe'}
    info = None
    dl = time.time() + 25
    while time.time() < dl:
        try:
            info = fr.evaluate("() => { const v=document.querySelector('video'); return v?{t:v.currentTime,d:v.duration}:null }")
        except Exception:
            info = None
        if info and isinstance(info.get('d'), (int, float)) and info['d'] == info['d'] and info['d'] > 0:
            break
        page.wait_for_timeout(1500)
    if not info:
        return {'ok': False, 'reason': 'novideoel'}
    dur = info['d']
    if not (isinstance(dur, (int, float)) and dur == dur and dur > 0):
        dur = float(fallback_dur or 0)
    if dur and info['t'] >= dur - 5:
        page.wait_for_timeout(4000)
        return {'ok': True, 'reason': 'already-done', 'dur': dur}
    r = fr.evaluate("async () => { const v=document.querySelector('video'); try{ await v.play(); }catch(e){ return 'playerr'; } return 'ok'; }")
    if r != 'ok':
        return {'ok': False, 'reason': r}
    page.wait_for_timeout(1500)
    if rate and rate > 1:
        fr.evaluate("(r) => { const v=document.querySelector('video'); try{ v.playbackRate=r; }catch(e){} }", rate)
    # 元数据缺失时用兜底时长; ended 事件优先判定完成
    deadline = time.time() + (dur / max(rate, 1)) * 2.0 + 420 if dur else time.time() + 3600
    last_t, stall = -1, 0
    while time.time() < deadline:
        try:
            st = fr.evaluate("() => { const v=document.querySelector('video'); return v?{t:v.currentTime,d:v.duration,ended:v.ended}:null }")
        except Exception:
            st = None
        if not st:
            return {'ok': False, 'reason': 'lostvideo'}
        sd = st['d']
        d_ok = isinstance(sd, (int, float)) and sd == sd and sd > 0
        if st['ended'] or (d_ok and st['t'] >= sd - 2) or (not d_ok and dur and st['t'] >= dur - 3):
            page.wait_for_timeout(9000)
            return {'ok': True, 'reason': 'finished', 'dur': sd if d_ok else dur}
        if abs(st['t'] - last_t) < 0.5:
            stall += 1
            if stall >= 6:
                fr.evaluate("() => { const v=document.querySelector('video'); try{v.play();}catch(e){} }")
                stall = 0
        else:
            stall = 0
        last_t = st['t']
        page.wait_for_timeout(2500)
    return {'ok': False, 'reason': 'timeout', 't': last_t}

def open_quiz_frame(page, qurl, attempts=5):
    """about:blank 隔离 + 重试加载测验 work frame。"""
    for att in range(1, attempts + 1):
        try:
            page.goto('about:blank', timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        try:
            page.goto(qurl, wait_until='domcontentloaded', timeout=45000)
        except Exception as e:
            log('    (重试%d) goto 失败: %s' % (att, str(e)[:60]))
            continue
        t0 = time.time()
        while time.time() - t0 < 22:
            for fr in page.frames:
                if 'doHomeWorkNew' in fr.url:
                    page.wait_for_timeout(2500)
                    return fr
            page.wait_for_timeout(1000)
        log('    (尝试%d) work iframe 未出现, 重试...' % att)
    return None

def extract_questions(fr):
    data = fr.evaluate("""() => {
        const out = [];
        document.querySelectorAll('.TiMu').forEach((tm, idx) => {
            const typeEl = tm.querySelector('.newZy_TItle');
            const titleEl = tm.querySelector('.Zy_TItle');
            const stemHtml = titleEl ? titleEl.innerHTML : '';
            const opts = [];
            tm.querySelectorAll('li[qid]').forEach(li => {
                const span = li.querySelector('.num_option');
                const a = li.querySelector('a');
                opts.push({ qid: li.getAttribute('qid'),
                            qtype: li.getAttribute('qtype'),
                            data: span ? span.getAttribute('data') : '',
                            label: span ? (span.textContent||'').trim() : '',
                            text: a ? (a.textContent||'').trim() : '' });
            });
            out.push({ idx: idx, type: typeEl ? (typeEl.textContent||'').trim() : '',
                       stemHtml: stemHtml, opts: opts });
        });
        return out;
    }""")
    for q in data:
        q['stem'] = decode(re.sub(r'<[^>]+>', ' ', q.get('stemHtml', '')))
        q['stem'] = re.sub(r'\s+', '', q['stem'])
        q['type'] = decode(q.get('type', ''))
        del q['stemHtml']
    return data

def render_font_grid(fr, ctx, tag='pending'):
    """在无头浏览器里渲染加密字体对照表截图 (不弹任何窗口)。
    返回 (字符数, 截图路径) 或 None。"""
    try:
        b64 = fr.evaluate("""() => {
            const styles = Array.from(document.querySelectorAll('style')).map(s=>s.textContent).join('\\n');
            const m = styles.match(/@font-face\\s*\\{[^}]*font-cxsecret[^}]*\\}/);
            if (!m) return null;
            const b = m[0].match(/base64,([A-Za-z0-9+/=]+)/);
            return b ? b[1] : null;
        }""")
    except Exception:
        b64 = None
    if not b64:
        return None
    try:
        font_path = BASE / ('cxsecret_%s.ttf' % tag)
        font_path.write_bytes(base64.b64decode(b64))
        cps = []
        try:
            from fontTools.ttLib import TTFont
            f = TTFont(str(font_path))
            cps = sorted(f.getBestCmap().keys())
        except Exception:
            # 回退: 从题目文本收集非 ASCII 字符
            for q in CURRENT_QUESTIONS:
                for ch in q.get('stem', '') + ''.join(o.get('text', '') for o in q.get('opts', [])):
                    if ord(ch) > 0x3400 and not ('\u4e00' <= ch <= '\u9fff'):
                        if ord(ch) not in cps:
                            cps.append(ord(ch))
            cps.sort()
        if not cps:
            return None
        cells = ''.join('<div class="cell"><div class="idx">%d</div><div class="ch">%s</div></div>'
                        % (i, chr(cp)) for i, cp in enumerate(cps))
        html = ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
                '@font-face{font-family:cxg;src:url(data:application/font-ttf;charset=utf-8;base64,%s) format("truetype");}'
                'body{margin:0;background:#fff;font-family:cxg}'
                '#grid{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;padding:10px}'
                '.cell{border:2px solid #333;text-align:center;padding:4px 0}'
                '.idx{font-size:13px;color:#c00;font-family:Arial}'
                '.ch{font-size:44px;line-height:1.1}'
                '</style></head><body><div id="grid">%s</div></body></html>' % (b64, cells))
        grid_path = BASE / ('font_grid_%s.html' % tag)
        grid_path.write_text(html, encoding='utf-8')
        shot = BASE / ('font_grid_%s.png' % tag)
        gpage = ctx.new_page()
        try:
            gpage.goto(grid_path.as_uri(), wait_until='load', timeout=15000)
            gpage.wait_for_timeout(600)
            gpage.screenshot(path=str(shot), full_page=True)
        finally:
            gpage.close()
        return len(cps), shot
    except Exception as e:
        return None

CURRENT_QUESTIONS = []

def apply_answers(fr, answers):
    """状态感知点选(幂等): 先检查选项当前勾选状态, 只点击需要改变的。
    answers = {qid: [want, ...]}, want 可为 label / 'opt:<索引>' / 选项文本"""
    return fr.evaluate("""(answers) => {
        const clicked = [];
        // 按 qid 分组: 共用案例题(一道大题含多个小题)每个小题有独立 qid
        const groups = {};
        document.querySelectorAll('li[qid]').forEach(li => {
            const q = li.getAttribute('qid');
            (groups[q] = groups[q] || []).push(li);
        });
        Object.keys(groups).forEach(qid => {
            const lis = groups[qid];
            const want = answers[qid];
            if (!want || !want.length) return;
            lis.forEach((li, idx) => {
                const span = li.querySelector('.num_option, .num_option_dx');
                const label = span ? (span.textContent||'').trim() : '';
                const a = li.querySelector('a');
                const text = a ? (a.textContent||'').trim() : '';
                let hit = false;
                for (const w of want) {
                    if (w === label) { hit = true; break; }
                    if (w.indexOf('opt:') === 0 && parseInt(w.slice(4)) === idx) { hit = true; break; }
                    if (w.length >= 4 && text && text.indexOf(w) >= 0) { hit = true; break; }
                }
                // 当前状态: 是否已勾选
                const checked = span ? span.classList.contains('check_answer') || span.classList.contains('check_answer_dx') : false;
                if (hit !== checked) {
                    li.click();
                    clicked.push(qid + ':#' + idx + (hit ? ':+' : ':-'));
                }
            });
        });
        return clicked;
    }""", answers)

def fill_blanks(fr, answers):
    """填空题: answers 值形如 'txt:根部', 通过 UEditor API 写入对应空。
    返回已填写的 qid 列表。"""
    pairs = []
    for qid, want in answers.items():
        for w in want:
            if isinstance(w, str) and w.startswith('txt:'):
                pairs.append([qid, w[4:]])
    if not pairs:
        return []
    return fr.evaluate("""(pairs) => {
        const done = [];
        for (const [qid, text] of pairs) {
            const ta = Array.from(document.querySelectorAll('textarea[name^="answerEditor"]'))
                .find(t => t.name.indexOf('answerEditor' + qid) === 0);
            if (!ta) continue;
            let ok = false;
            try {
                if (window.UE && UE.getEditor) {
                    const ed = UE.getEditor(ta.name);
                    if (ed) { ed.setContent(text); ok = true; }
                }
            } catch (e) {}
            if (!ok) {
                ta.value = text;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
            }
            done.push(qid);
        }
        return done;
    }""", pairs)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rate', type=float, default=2)
    ap.add_argument('--max-leaves', type=int, default=0)
    ap.add_argument('--quiz-wait-min', type=int, default=45)
    ap.add_argument('--workdir', default='', help='课程工作区目录(含 course_config.json/cookies.txt/required_leaves.json)')
    ap.add_argument('--no-recheck-prev', action='store_true',
                    help='未解锁时不重载上一节页面核对(避免访问旧资源), 直接停止')
    args = ap.parse_args()
    workdir = args.workdir or os.environ.get('XXW_WORKDIR', '')
    if not workdir:
        print('需要 --workdir 或环境变量 XXW_WORKDIR 指定工作目录(含 course_config.json/cookies.txt/required_leaves.json)',
              file=sys.stderr)
        return 2
    init_course_context(Path(workdir))
    leaves = json.loads((BASE / 'required_leaves.json').read_text(encoding='utf-8'))
    st = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {
        'videosDone': 0, 'quizzesSubmitted': 0, 'doneLeaves': [], 'blockedAt': None}
    s = rq_session()
    log('=== 必修遍历 v4 启动: 叶子 %d, 已完成 %d, 已交测验 %d ===' %
        (len(leaves), len(st.get('doneLeaves', [])), st.get('quizzesSubmitted', 0)))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME,
                                    args=['--autoplay-policy=no-user-gesture-required', '--mute-audio',
                                          '--no-first-run', '--no-default-browser-check'])
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()
        page.on('dialog', lambda d: (log('  [dialog] ' + (d.message or '')[:80]), d.accept()))
        load_cookies(ctx)
        blocked = False
        for i, lf in enumerate(leaves, 1):
            if args.max_leaves and i > args.max_leaves:
                log('-- 达到 max-leaves, 停止 --')
                break
            kid, name = lf['id'], lf['name']
            if kid in st.get('doneLeaves', []):
                continue
            h0 = cards_page(s, kid, 0)
            if not h0 or ('mArg' not in h0 and 'TiMu' not in h0):
                # 用户指令: 通过但未解锁时, 重载上一节页面核对通过状态, 再重试一次; 仍未解锁则停止
                if i >= 2 and not args.no_recheck_prev:
                    log('◇ [%d] %s 未解锁, 重载上一节核对通过状态...' % (i, name))
                    try:
                        prev = leaves[i - 2]
                        recheck_prev(s, prev['id'], prev.get('name', '?'))
                    except Exception as e:
                        log('  ? 核对上一节异常: %s' % str(e)[:60])
                    page.wait_for_timeout(30000)
                    h0 = cards_page(s, kid, 0)
                    if h0 and ('mArg' in h0 or 'TiMu' in h0):
                        log('✔ [%d] %s 重载核对后已解锁, 继续处理' % (i, name))
                    else:
                        log('⛔ [%d] %s %s 重载核对后仍未解锁, 停止运行' % (i, kid, name))
                        st['blockedAt'] = {'kid': kid, 'name': name, 'index': i}
                        blocked = True
                        break
                else:
                    log('⛔ [%d] %s %s 未解锁, 停止 (需先完成上一节任务点)' % (i, kid, name))
                    st['blockedAt'] = {'kid': kid, 'name': name, 'index': i}
                    blocked = True
                    break
            log('▶ [%d/%d] %s %s' % (i, len(leaves), kid, name))
            num = 0
            leaf_ok = True
            while True:
                h = h0 if num == 0 else cards_page(s, kid, num)
                if not h or ('mArg' not in h and 'TiMu' not in h and 'module=' not in h):
                    break
                if 'insertvideo' in h or re.search(r'attDuration.:\s*\d+', h):
                    states = video_states(h)
                    if not states:
                        log('  ? 视频卡%d 解析不出视频附件, 按单个播放处理' % num)
                        need = [(0, 0)]
                    else:
                        # 单卡可能内嵌多个视频: 逐个判断哪些需播放
                        need = []
                        for vi, (oid, dur, job, passed) in enumerate(states):
                            if job is True and not passed:
                                need.append((vi, dur))
                            elif job is None and passed:
                                log('  ✔ 视频卡%d 子项%d 已完成(isPassed), 跳过' % (num, vi + 1))
                            else:
                                log('  ◇ 视频卡%d 子项%d 非任务点(job=None), 跳过' % (num, vi + 1))
                        if not need:
                            log('  ✔ 视频卡%d 所有子项均完成/跳过' % num)
                    if need:
                        vurl = ('https://%s/mooc-ans/knowledge/cards?clazzid=%d&courseid=%d'
                                '&knowledgeid=%s&num=%d&ut=s&cpi=&v=2025-0424-1038-3&mooc2=1'
                                % (DOMAIN, CLAZZ_ID, COURSE_ID, kid, num))
                        try:
                            page.goto('about:blank', timeout=15000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1200)
                        page.goto(vurl, wait_until='domcontentloaded', timeout=45000)
                        page.wait_for_timeout(2500)
                        all_ok = True
                        for idx, dur in need:
                            fr = find_video_frame_at(page, idx)
                            res = play_video_frame(page, fr, args.rate, fallback_dur=dur) if fr else {'ok': False, 'reason': 'novideoframe'}
                            if res.get('ok'):
                                st['videosDone'] = st.get('videosDone', 0) + 1
                                log('  ✔ 视频卡%d 子项%d 播放完成 (%s, %.0fs)' % (num, idx + 1, res.get('reason'), res.get('dur') or 0))
                            else:
                                all_ok = False
                                log('  ✘ 视频卡%d 子项%d 失败: %s' % (num, idx + 1, res.get('reason')))
                        if not all_ok:
                            leaf_ok = False
                        page.wait_for_timeout(1500)
                elif 'insertdoc' in h:
                    docs = doc_jobs(h)
                    if not docs:
                        log('  ✔ 文档卡%d 无未完成任务点, 跳过' % num)
                    for a in docs:
                        dname = str((a.get('property') or {}).get('name', ''))[:26]
                        ok = finish_doc_job(s, kid, a)
                        if ok:
                            st['docsDone'] = st.get('docsDone', 0) + 1
                            log('  ✔ 文档卡%d 任务点已完成: %s' % (num, dname))
                            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
                        else:
                            log('  ✘ 文档卡%d 任务点未完成: %s' % (num, dname))
                            leaf_ok = False
                    page.wait_for_timeout(1500)
                elif 'module="work"' in h:
                    qurl = ('https://%s/mooc-ans/knowledge/cards?clazzid=%d&courseid=%d'
                            '&knowledgeid=%s&num=%d&ut=s&cpi=&v=2025-0424-1038-3&mooc2=1'
                            % (DOMAIN, CLAZZ_ID, COURSE_ID, kid, num))
                    fr = open_quiz_frame(page, qurl)
                    if not fr:
                        log('  ✘ 测验卡%d: work iframe 3次尝试均未加载' % num)
                        leaf_ok = False
                    else:
                        # 题目在 iframe 内 XHR 异步加载, 首次提取可能为空 ->
                        # 重试提取, 避免把「加载中」误判为「已提交过」
                        qs = []
                        for _ext in range(5):
                            qs = extract_questions(fr)
                            if qs:
                                break
                            page.wait_for_timeout(4000)
                        if not qs:
                            try:
                                diag = fr.evaluate("""() => ({
                                    url: location.href.slice(0, 80),
                                    len: document.body ? document.body.innerText.length : 0,
                                    head: document.body ? document.body.innerText.replace(/\\s+/g, ' ').slice(0, 100) : ''})""")
                                log('  ? 测验卡%d 空题诊断: len=%s head=%s' % (num, diag.get('len'), diag.get('head', '')))
                            except Exception as e:
                                log('  ? 空题诊断失败: %s' % str(e)[:60])
                            log('  ✔ 测验卡%d 无题目(已提交过), 跳过' % num)
                        else:
                            global CURRENT_QUESTIONS
                            CURRENT_QUESTIONS = qs
                            pending = {'kid': kid, 'name': name, 'num': num,
                                       'questions': [{k: q[k] for k in ('idx', 'type', 'stem', 'opts')} for q in qs]}
                            PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf-8')
                            # 无头渲染字体对照表 (供解码, 不弹窗)
                            grid = None
                            try:
                                grid = render_font_grid(fr, ctx, 'pending')
                            except Exception as e:
                                log('  ! 字体对照表渲染失败: %s' % str(e)[:60])
                            if grid:
                                log('  ◇ 字体对照表已生成: %d 字符 -> %s' % (grid[0], grid[1].name))
                            log('  ◇ 测验卡%d 出题 %d 道, 等待答案(最多%d分钟)...' % (num, len(qs), args.quiz_wait_min))
                            ans = None
                            dl = time.time() + args.quiz_wait_min * 60
                            while time.time() < dl:
                                if ANSWERS.exists():
                                    try:
                                        ans = json.loads(ANSWERS.read_text(encoding='utf-8'))
                                        break
                                    except Exception:
                                        pass
                                page.wait_for_timeout(5000)
                            if not ans:
                                log('  ⏱ 等待答案超时, 停止')
                                st['blockedAt'] = {'kid': kid, 'name': name, 'index': i, 'reason': 'quiz-wait'}
                                blocked = True
                                break
                            clicked = apply_answers(fr, ans)
                            log('  -> 已点选 %d 项: %s' % (len(clicked), ','.join(clicked[:8])))
                            blanks = fill_blanks(fr, ans)
                            if blanks:
                                log('  -> 已填写填空 %d 处: %s' % (len(blanks), ','.join(blanks)))
                            page.wait_for_timeout(1500)
                            # 校验: 检查 answer 输入框是否已填充(防止旧 frame 引用导致点击失效)
                            try:
                                filled = fr.evaluate("""() => {
                                    const out = [];
                                    document.querySelectorAll('input[name^="answer"]').forEach(i => {
                                        if (i.name.indexOf('answertype') !== 0 && i.name !== 'answerwqbid' && i.name !== 'answerId') {
                                            out.push(i.name + '=' + (i.value || ''));
                                        }
                                    });
                                    return out;
                                }""")
                                empty = [x for x in filled if x.endswith('=')]
                                if empty:
                                    log('  ! 发现未填充的答案框: %s, 重新定位 frame 并重点' % empty)
                                    # 重新查找 work frame
                                    fr2 = None
                                    for f in page.frames:
                                        if 'doHomeWorkNew' in f.url:
                                            fr2 = f
                                            break
                                    if fr2:
                                        fr = fr2
                                        clicked = apply_answers(fr, ans)
                                        log('  -> 重新点选 %d 项' % len(clicked))
                                        page.wait_for_timeout(1500)
                                        filled2 = fr.evaluate("""() => {
                                            const out = [];
                                            document.querySelectorAll('input[name^="answer"]').forEach(i => {
                                                if (i.name.indexOf('answertype') !== 0 && i.name !== 'answerwqbid' && i.name !== 'answerId') {
                                                    out.push(i.name + '=' + (i.value || ''));
                                                }
                                            });
                                            return out;
                                        }""")
                                        log('  -> 答案框状态: %s' % filled2)
                                else:
                                    log('  -> 答案框已填充: %s' % filled)
                            except Exception as e:
                                log('  ! 校验失败: %s' % str(e)[:60])
                            # 补齐 toadd 流程的必填字段 (answerwqbid 等)
                            try:
                                qid_list = fr.evaluate("""() => {
                                    const names = Array.from(document.querySelectorAll('input[name^="answertype"]'))
                                        .map(i => i.name.replace('answertype',''));
                                    // 共用案例题(qtype=15)的子题 qid 形如 <父题号>-<uuid>:
                                    // 须调用平台 setReadComprehensionAnswer() 生成整题 JSON, 题号串用父题号
                                    if (names.some(n => n.indexOf('-') >= 0)) {
                                        try { if (typeof setReadComprehensionAnswer === 'function') setReadComprehensionAnswer(); } catch (e) {}
                                        const parents = [];
                                        names.forEach(n => {
                                            const p = n.split('-')[0];
                                            if (parents.indexOf(p) < 0) parents.push(p);
                                        });
                                        const qids2 = parents.join(',') + ',';
                                        const wq1 = document.querySelector('#answerwqbid');
                                        if (wq1) wq1.value = qids2;
                                        return qids2;
                                    }
                                    const qids = names.join(',') + ',';
                                    const wq = document.querySelector('#answerwqbid');
                                    if (wq) wq.value = qids;
                                    const pf = document.querySelector('#pyFlag');
                                    if (pf) pf.value = '';
                                    const enc = document.querySelector('#enc');
                                    if (enc) enc.value = '';
                                    return qids;
                                }""")
                                log('  -> answerwqbid = %s' % qid_list)
                            except Exception as e:
                                log('  ! 补齐字段失败: %s' % str(e)[:60])
                            # 提交并拦截响应
                            submit_result = None
                            submit_ok = False
                            try:
                                with page.expect_response(lambda r: 'addStudentWorkNewWeb' in r.url, timeout=25000) as ri:
                                    # 共用案例题(qtype=15)必须走界面提交(平台 toadd 未正确收集子题答案)
                                    is_case = fr.evaluate("""() => Array.from(document.querySelectorAll('input[name^="answertype"]'))
                                        .some(i => i.name.replace('answertype','').indexOf('-') >= 0)""")
                                    if is_case:
                                        fr.evaluate("() => { const e = document.querySelector('.btnSubmit'); if (e) e.click(); }")
                                        page.wait_for_timeout(1200)
                                        fr.evaluate("() => { const el = document.querySelector('#popok'); if (el) el.click(); }")
                                    else:
                                        fr.evaluate("() => { if (typeof submitCheckTimes === 'function') submitCheckTimes(); }")
                                resp = ri.value
                                submit_result = resp.text()[:300]
                                try:
                                    rj = json.loads(resp.text())
                                    submit_ok = bool(rj.get('status'))
                                    if not submit_ok:
                                        log('  ✘ 服务端拒绝: %s' % rj.get('msg', ''))
                                except Exception:
                                    pass
                            except Exception as e:
                                log('  ! 提交响应未捕获: %s' % str(e)[:60])
                            if submit_result:
                                log('  -> 提交响应: %s' % submit_result.replace('\n', ' ')[:180])
                            page.wait_for_timeout(4000)
                            if not submit_ok:
                                # 失败: 不标记提交, 停止等待人工处理
                                log('  ⛔ 测验提交失败, 停止遍历')
                                st['blockedAt'] = {'kid': kid, 'name': name, 'index': i, 'reason': 'submit-failed'}
                                blocked = True
                                ANSWERS.unlink(missing_ok=True)
                                PENDING.unlink(missing_ok=True)
                                STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
                                break
                            # 可能的确认弹窗
                            try:
                                fr.evaluate("""() => {
                                    const el = document.querySelector('#popok');
                                    if (el && el.offsetParent !== null) el.click();
                                }""")
                            except Exception:
                                pass
                            page.wait_for_timeout(3000)
                            st['quizzesSubmitted'] = st.get('quizzesSubmitted', 0) + 1
                            log('  ✔ 测验卡%d 已提交成功' % num)
                            ANSWERS.unlink(missing_ok=True)
                            PENDING.unlink(missing_ok=True)
                            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
                num += 1
                if num > 6 or blocked:
                    break
            if blocked:
                break
            if leaf_ok:
                st.setdefault('doneLeaves', []).append(kid)
            STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
        if not blocked:
            log('=== 必修全部叶子处理完成 ===')
        log('统计: 视频 %d, 测验提交 %d, 完成节 %d/%d' %
            (st.get('videosDone', 0), st.get('quizzesSubmitted', 0),
             len(st.get('doneLeaves', [])), len(leaves)))
        browser.close()

if __name__ == '__main__':
    main()