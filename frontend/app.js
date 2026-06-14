const { createApp, ref, reactive, computed, onMounted, watch } = Vue;
const BASE = '/devbot';

createApp({
  setup() {
    const token = ref(localStorage.getItem('devbot_token') || '');
    const username = ref(localStorage.getItem('devbot_username') || '');
    const authTab = ref('login');
    const authForm = reactive({ username: '', password: '' });
    const authLoading = ref(false);

    const currentPage = ref('review');
    const sidebarOpen = ref(false);
    const toasts = ref([]);
    function showToast(message, type='info') {
      const t = { message, type };
      toasts.value.push(t);
      setTimeout(()=>{ const i=toasts.value.indexOf(t); if(i>=0) toasts.value.splice(i,1); }, 3500);
    }
    function pageLabel(p){ return ({review:'新建评审',history:'评审历史',github:'GitHub 仓库',webhook:'Webhook 设置'})[p] || p; }
    function goPage(p){
      currentPage.value=p;
      // Auto-close mobile drawer
      if (window.innerWidth <= 768) sidebarOpen.value = false;
      if(p==='history') loadHistory();
    }

    async function api(method, path, body=null) {
      const opts = { method, headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token.value } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(BASE + path, opts);
      let data;
      try { data = await r.json(); } catch { data = {}; }
      if (!r.ok) {
        const msg = data.detail || data.error || '请求失败';
        if (r.status === 401) doLogout();
        throw new Error(msg);
      }
      return data;
    }

    async function doLogin() {
      authLoading.value = true;
      try {
        const data = await fetch(BASE + '/api/v1/auth/login', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(authForm)
        }).then(r => r.json().then(d => { if (!r.ok) throw new Error(d.detail || '登录失败'); return d; }));
        token.value = data.token; username.value = data.username;
        localStorage.setItem('devbot_token', data.token);
        localStorage.setItem('devbot_username', data.username);
        showToast('欢迎回来 · ' + data.username, 'success');
      } catch (e) { showToast(e.message, 'error'); }
      authLoading.value = false;
    }
    async function doRegister() {
      authLoading.value = true;
      try {
        const data = await fetch(BASE + '/api/v1/auth/register', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(authForm)
        }).then(r => r.json().then(d => { if (!r.ok) throw new Error(d.detail || '注册失败'); return d; }));
        token.value = data.token; username.value = data.username;
        localStorage.setItem('devbot_token', data.token);
        localStorage.setItem('devbot_username', data.username);
        showToast('注册成功 · 欢迎使用', 'success');
      } catch (e) { showToast(e.message, 'error'); }
      authLoading.value = false;
    }
    function doLogout() {
      token.value=''; username.value='';
      localStorage.removeItem('devbot_token');
      localStorage.removeItem('devbot_username');
    }

    // Review
    const reviewMode = ref('manual');
    const reviewForm = reactive({ repo:'', prNumber:'', diff:'', title:'', language:'python' });
    const fetchingPR = ref(false);
    const reviewing = ref(false);
    // Smart URL input state
    const reviewUrl = ref('');
    const diffData = ref(null);
    const urlLoading = ref(false);

    async function fetchFromUrl() {
      const u = reviewUrl.value.trim();
      if (!u) { showToast('请粘贴 PR 链接或 owner/repo#N', 'error'); return; }
      urlLoading.value = true;
      try {
        const r = await api('POST', '/api/v1/review/from-url', { url: u });
        // Normalize language for select
        const langMap = { python:'python', java:'java', go:'go', typescript:'typescript', javascript:'javascript', rust:'rust', 'c++':'cpp', cpp:'cpp' };
        const detected = (r.language || '').toLowerCase();
        r.language = langMap[detected] || '';
        diffData.value = r;
        reviewResult.value = null;
        taskId.value = null;
        showToast(`已拉取 ${r.stats.files} 个文件的 diff（+${r.stats.additions} / -${r.stats.deletions}）`, 'success');
      } catch (e) {
        showToast('拉取失败: ' + e.message, 'error');
      } finally {
        urlLoading.value = false;
      }
    }

    async function submitReview() {
      if (!diffData.value || !diffData.value.diff) {
        showToast('请先拉取 diff', 'error');
        return;
      }
      reviewing.value = true;
      reviewResult.value = null;
      taskId.value = null;
      taskStatus.value = '';
      try {
        const data = await api('POST', '/api/v1/review', {
          pr_id: diffData.value.pr_id,
          diff: diffData.value.diff,
          title: diffData.value.title || '',
          language: diffData.value.language || 'python',
        });
        taskId.value = data.task_id;
        taskStatus.value = data.status;
        showToast('评审任务已提交', 'success');
        pollTaskStatus();
      } catch (e) {
        showToast('提交失败: ' + e.message, 'error');
        reviewing.value = false;
      }
    }
    // Diff file upload state
    const diffFileInput = ref(null);
    const uploadedDiffFileName = ref('');
    const uploadedDiffFileSize = ref('');
    const diffDragOver = ref(false);

    function humanSize(n) {
      if (n < 1024) return n + ' B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
      return (n / 1024 / 1024).toFixed(2) + ' MB';
    }
    function clearDiffFile() {
      uploadedDiffFileName.value = '';
      uploadedDiffFileSize.value = '';
    }
    function onDiffFilePicked(e) {
      const f = e.target.files && e.target.files[0];
      if (f) acceptDiffFile(f);
      e.target.value = '';
    }
    function onDiffFileDropped(e) {
      diffDragOver.value = false;
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) acceptDiffFile(f);
    }
    function acceptDiffFile(f) {
      if (!/\.(diff|patch)$/i.test(f.name)) {
        showToast('请选择 .diff 或 .patch 文件', 'error');
        return;
      }
      if (f.size > 5 * 1024 * 1024) {
        showToast('文件大小不能超过 5MB', 'error');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        reviewForm.diff = reader.result || '';
        uploadedDiffFileName.value = f.name;
        uploadedDiffFileSize.value = humanSize(f.size);
        if (!reviewForm.title) reviewForm.title = f.name;
        showToast('已加载 ' + f.name + '，可点击下方"开始评审"', 'success');
      };
      reader.onerror = () => showToast('读取文件失败', 'error');
      reader.readAsText(f, 'utf-8');
    }
    const taskId = ref(null);
    const taskStatus = ref('');
    const reviewResult = ref(null);
    let pollTimer = null;

    const criticNames = ['correctness','design','security','readability'];
    const criticLabels = { correctness:'正确性', design:'架构设计', security:'安全性', readability:'可读性' };
    const criticIcons  = { correctness:'C', design:'D', security:'S', readability:'R' };

    function riskColor(s){ if(s>=70) return '#f43f5e'; if(s>=40) return '#f59e0b'; return '#10b981'; }
    function riskGradient(s){
      if(s>=70) return '#ff3b30';
      if(s>=40) return '#ff9f0a';
      return '#34c759';
    }
    function riskGradientUrl(s){
      if(s>=70) return '#ff3b30';
      if(s>=40) return '#ff9f0a';
      return '#34c759';
    }
    function riskLevelLabel(l){ return ({LOW:'低风险 · 可合并', MEDIUM:'中风险 · 建议复核', HIGH:'高风险 · 需要人工审查'})[l] || l; }
    function totalFindings(r){ if(!r||!r.critics) return 0; return r.critics.reduce((a,c)=>a+(c.findings?c.findings.length:0),0); }

    function parseDiffLines(text){
      const lines = (text||'').split('\n');
      const out = [];
      let oldNo = 0, newNo = 0;
      for (const line of lines) {
        if (line.startsWith('@@')) {
          const m = line.match(/@@ -(\d+),?\d* \+(\d+),?\d* @@/);
          if (m) { oldNo = parseInt(m[1]) - 1; newNo = parseInt(m[2]) - 1; }
          out.push({ kind:'hunk', no:'', text:line });
        } else if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) {
          out.push({ kind:'meta', no:'', text:line });
        } else if (line.startsWith('+') && !line.startsWith('+++')) {
          newNo++; out.push({ kind:'add', no:newNo, text:line });
        } else if (line.startsWith('-') && !line.startsWith('---')) {
          oldNo++; out.push({ kind:'del', no:oldNo, text:line });
        } else {
          oldNo++; newNo++; out.push({ kind:'ctx', no:newNo, text:line });
        }
      }
      return out;
    }

    async function fetchPR() {
      const repo = reviewForm.repo.trim();
      const pr = reviewForm.prNumber;
      if (!repo || !pr) { showToast('请填写仓库和 PR 编号', 'error'); return; }
      if (!repo.includes('/')) { showToast('仓库格式应为 owner/repo', 'error'); return; }
      fetchingPR.value = true;
      try {
        const data = await api('GET', `/api/v1/github/${repo}/pulls/${pr}`);
        if (data.error) throw new Error(data.error);
        reviewForm.diff = data.diff || '';
        reviewForm.title = data.title || '';
        showToast(`已拉取 PR #${pr}（+${data.additions||0} / -${data.deletions||0}）`, 'success');
      } catch(e) { showToast('拉取失败: '+e.message, 'error'); }
      fetchingPR.value = false;
    }

    
    const ghOwnerRepo = ref('');
    const ghRepo = ref(null);
    const ghPrs = ref([]);
    const ghCommits = ref([]);
    const ghBranches = ref([]);
    const ghTab = ref('prs');
    const ghLoading = ref(false);

    function fmtGhDate(s) {
      if (!s) return '';
      const d = new Date(s);
      const now = new Date();
      const ms = now - d;
      if (ms < 60000) return '刚刚';
      if (ms < 3600000) return Math.floor(ms/60000) + '分钟前';
      if (ms < 86400000) return Math.floor(ms/3600000) + '小时前';
      if (ms < 604800000) return Math.floor(ms/86400000) + '天前';
      return d.toISOString().slice(0,10);
    }

    async function loadGhRepo() {
      const v = ghOwnerRepo.value.trim();
      if (!v.includes('/')) { toast.value = '格式：owner/repo'; return; }
      const [owner, repo] = v.split('/');
      ghLoading.value = true;
      try {
        const info = await api('/api/v1/github/' + owner + '/' + repo + '/info');
        ghRepo.value = info;
        await setGhTab('prs');
      } catch(e) {
        toast.value = '加载失败: ' + e.message;
        ghRepo.value = null;
      } finally {
        ghLoading.value = false;
      }
    }

    async function setGhTab(tab) {
      ghTab.value = tab;
      if (!ghRepo.value) return;
      const [owner, repo] = ghOwnerRepo.value.trim().split('/');
      try {
        if (tab === 'prs') {
          const r = await api('/api/v1/github/' + owner + '/' + repo + '/prs?state=open');
          ghPrs.value = r.prs || [];
        } else if (tab === 'commits') {
          const r = await api('/api/v1/github/' + owner + '/' + repo + '/commits');
          ghCommits.value = r.commits || [];
        } else if (tab === 'branches') {
          const r = await api('/api/v1/github/' + owner + '/' + repo + '/branches');
          ghBranches.value = r.branches || [];
        }
      } catch(e) {
        toast.value = '加载失败: ' + e.message;
      }
    }

    async function reviewGhPr(pr) {
      const v = ghOwnerRepo.value.trim();
      try {
        const r = await api('/api/v1/review/from-url', {
          method: 'POST',
          body: JSON.stringify({ url: v + '#' + pr.number }),
        });
        await api('/api/v1/review', {
          method: 'POST',
          body: JSON.stringify({ pr_id: r.pr_id, diff: r.diff, title: r.title, language: r.language }),
        });
        toast.value = 'PR #' + pr.number + ' 已加入评审队列';
        setTimeout(() => toast.value = '', 3500);
      } catch(e) { toast.value = '提交失败: ' + e.message; }
    }

    async function reviewGhCommit(c) {
      const v = ghOwnerRepo.value.trim();
      try {
        const r = await api('/api/v1/review/from-url', {
          method: 'POST',
          body: JSON.stringify({ url: 'https://github.com/' + v + '/commit/' + c.sha }),
        });
        await api('/api/v1/review', {
          method: 'POST',
          body: JSON.stringify({ pr_id: r.pr_id, diff: r.diff, title: r.title, language: r.language }),
        });
        toast.value = 'Commit ' + c.short_sha + ' 已加入评审队列';
        setTimeout(() => toast.value = '', 3500);
      } catch(e) { toast.value = '提交失败: ' + e.message; }
    }

    async function startReview() {
      const diff = reviewForm.diff.trim();
      if (!diff) { showToast('请先填入 diff 内容', 'error'); return; }
      reviewing.value = true;
      reviewResult.value = null;
      taskId.value = null;
      taskStatus.value = '';
      const prId = (reviewForm.repo && reviewForm.prNumber) ? `${reviewForm.repo}#${reviewForm.prNumber}` : 'manual-review-' + Date.now().toString(36).slice(-4);
      try {
        const data = await api('POST', '/api/v1/review', {
          pr_id: prId, diff, title: reviewForm.title, language: reviewForm.language,
        });
        taskId.value = data.task_id;
        taskStatus.value = data.status;
        pollTaskStatus();
      } catch(e) {
        showToast('提交失败: ' + e.message, 'error');
        reviewing.value = false;
      }
    }

    function pollTaskStatus() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        try {
          const data = await api('GET', `/api/v1/review/${taskId.value}/status`);
          taskStatus.value = data.status;
          if (data.status === 'done') {
            clearInterval(pollTimer); pollTimer = null;
            reviewResult.value = data.result;
            reviewing.value = false;
            showToast('评审完成', 'success');
          } else if (data.status === 'error') {
            clearInterval(pollTimer); pollTimer = null;
            reviewing.value = false;
            showToast('评审出错: ' + (data.error || '未知错误'), 'error');
          }
        } catch (e) {
          clearInterval(pollTimer); pollTimer = null;
          reviewing.value = false;
        }
      }, 1500);
    }

    // History
    const historyItems = ref([]);
    const historyTotal = ref(0);
    const historyPage = ref(1);
    const historyPageSize = ref(20);
    const historyLoading = ref(false);
    const expandedHistoryId = ref(null);
    const sortKey = ref('created_at');
    const sortAsc = ref(false);

    async function loadHistory() {
      historyLoading.value = true;
      try {
        const data = await api('GET', `/api/v1/reviews?page=${historyPage.value}&page_size=${historyPageSize.value}`);
        historyItems.value = data.items || [];
        historyTotal.value = data.total || 0;
      } catch(e) { showToast('加载历史失败: ' + e.message, 'error'); }
      historyLoading.value = false;
    }
    function setSort(k){ if (sortKey.value === k) sortAsc.value = !sortAsc.value; else { sortKey.value = k; sortAsc.value = true; } }
    const sortedHistory = computed(() => {
      const arr = [...historyItems.value];
      arr.sort((a, b) => {
        const av = a[sortKey.value]; const bv = b[sortKey.value];
        if (av === bv) return 0;
        const r = av > bv ? 1 : -1;
        return sortAsc.value ? r : -r;
      });
      return arr;
    });

    function formatDate(s){
      if (!s) return '';
      try {
        const d = new Date(s.replace(' ','T') + (s.includes('Z')?'':'Z'));
        const now = new Date();
        const diff = now - d;
        if (diff < 60000) return '刚刚';
        if (diff < 3600000) return Math.floor(diff/60000) + ' 分钟前';
        if (diff < 86400000) return Math.floor(diff/3600000) + ' 小时前';
        return d.toLocaleDateString('zh-CN', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
      } catch { return s; }
    }

    // Webhook
    const webhookTesting = ref(false);
    const webhookTestResult = ref(null);
    const webhookSteps = [
      '打开 GitHub 仓库 → <b>Settings</b> → <b>Webhooks</b> → <b>Add webhook</b>',
      '<b>Payload URL</b> 中填写上方的 Webhook 地址',
      '<b>Content type</b> 选择 <code>application/json</code>',
      '<b>Secret</b> 填写你配置的密钥（默认 <code>dev-secret</code>）',
      '在事件选择中，勾选 <b>Pull requests</b>',
      '点击 <b>Add webhook</b> 完成配置',
    ];
    function copyWebhookUrl(){
      navigator.clipboard.writeText('https://124.222.50.21/devbot/webhook/github').then(
        () => showToast('已复制到剪贴板', 'success'),
        () => showToast('复制失败', 'error')
      );
    }
    async function testWebhook(){
      webhookTesting.value = true; webhookTestResult.value = null;
      try {
        const r = await fetch(BASE + '/health');
        const data = await r.json();
        webhookTestResult.value = { ok: data.status === 'ok', message: data.status === 'ok' ? '服务运行正常，Webhook 端点已就绪' : '服务异常: ' + JSON.stringify(data) };
      } catch(e) { webhookTestResult.value = { ok:false, message:'连接失败: '+e.message }; }
      webhookTesting.value = false;
    }

    watch(currentPage, (p) => { if (p === 'history') loadHistory(); });

    onMounted(() => {
      if (token.value) api('GET','/api/v1/reviews?page=1&page_size=1').catch(()=>{});
    });

    return {
      token, username, authTab, authForm, authLoading,
      doLogin, doRegister, doLogout,
      currentPage, sidebarOpen, toasts, pageLabel, goPage,
      reviewMode, ghOwnerRepo, ghRepo, ghPrs, ghCommits, ghBranches, ghTab, ghLoading, loadGhRepo, setGhTab, reviewGhPr, reviewGhCommit, fmtGhDate, reviewForm, fetchingPR, reviewing,
      reviewUrl, diffData, urlLoading, fetchFromUrl, submitReview,
      diffFileInput, uploadedDiffFileName, uploadedDiffFileSize, diffDragOver,
      onDiffFilePicked, onDiffFileDropped, clearDiffFile,
      taskId, taskStatus, reviewResult,
      criticNames, criticLabels, criticIcons,
      fetchPR, startReview, parseDiffLines, totalFindings,
      historyItems, historyTotal, historyPage, historyPageSize, historyLoading, expandedHistoryId,
      sortKey, sortAsc, sortedHistory, setSort, loadHistory,
      webhookTesting, webhookTestResult, webhookSteps,
      copyWebhookUrl, testWebhook,
      riskColor, riskGradient, riskGradientUrl, riskLevelLabel, formatDate,
    };
  }
}).mount('#app');
