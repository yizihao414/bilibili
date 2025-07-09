console.log('B站评论扩展与采集器 启动 V6（终极极速优化版）');

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// 通知 popup 页面状态
const updateStatus = (message) => {
    console.log(message);
    chrome.runtime.sendMessage({ action: 'updateStatus', message });
};

// 滚动到底部以加载所有评论
async function scrollToBottom() {
    updateStatus('1/3 正在滚动页面加载更多主评论...');
    let lastHeight = document.body.scrollHeight;
    let unchanged = 0;
    while (unchanged < 3) {
        window.scrollTo(0, document.body.scrollHeight);
        await sleep(1200);
        const newHeight = document.body.scrollHeight;
        if (newHeight === lastHeight) unchanged++;
        else unchanged = 0;
        lastHeight = newHeight;
    }
    updateStatus('主评论加载完成。');
}

// 提取评论文本
function getCommentText(renderer) {
    try {
        const shadow = renderer.shadowRoot;
        if (!shadow) return '';
        let rich = shadow.querySelector('bili-rich-text');
        if (!rich) {
            const content = shadow.querySelector('#content');
            if (content) rich = content.querySelector('bili-rich-text');
        }
        if (!rich) return '';
        if (rich.shadowRoot) {
            const temp = document.createElement('div');
            temp.innerHTML = rich.shadowRoot.innerHTML;
            temp.querySelectorAll('style').forEach(s => s.remove());
            return temp.textContent.trim();
        } else {
            return rich.textContent.trim();
        }
    } catch (e) {
        return '提取失败';
    }
}

// 查找下一页按钮
async function findNextButton(shadowRoot) {
    // 尝试 bili-pagination
    const pagination = shadowRoot.querySelector('bili-pagination');
    if (pagination?.shadowRoot) {
        const next = pagination.shadowRoot.querySelector('button[aria-label="下一页"], .bili-pagination__next');
        if (next && !next.disabled) return next;
    }
    // 尝试 bili-text-button
    const buttons = shadowRoot.querySelectorAll('bili-text-button');
    for (const btn of buttons) {
        if (btn.textContent.includes('下一页')) {
            if (btn.shadowRoot) {
                const inner = btn.shadowRoot.querySelector('button');
                if (inner && !inner.disabled) return inner;
            }
            return btn;
        }
    }
    return null;
}

// 收集当前页所有回复信息
function getCurrentReplies(shadowRoot) {
    const replies = [];
    const elements = shadowRoot.querySelectorAll('bili-comment-reply-renderer, bili-comment-renderer');
    for (const el of elements) {
        const txt = getCommentText(el);
        if (txt) replies.push(txt);
    }
    return replies;
}

// 展开单条评论的所有回复并采集
async function processReplies(threadShadow, i) {
    const container = threadShadow.querySelector('bili-comment-replies-renderer');
    if (!container?.shadowRoot) return [];

    const root = container.shadowRoot;
    let allReplies = [];

    // 初始展开
    let tryExpand = true;
    while (tryExpand) {
        tryExpand = false;
        const btns = root.querySelectorAll('bili-text-button');
        for (const btn of btns) {
            const txt = btn.textContent.trim();
            if (/点击查看|展开更多/.test(txt) && !txt.includes('下一页')) {
                if (btn.shadowRoot) {
                    const b = btn.shadowRoot.querySelector('button');
                    if (b) b.click();
                    else btn.click();
                } else {
                    btn.click();
                }
                tryExpand = true;
                await sleep(800);
                break;
            }
        }
    }

    // 翻页并采集
    let page = 1;
    while (true) {
        updateStatus(`楼层 ${i + 1}：采集第 ${page++} 页回复...`);
        allReplies.push(...getCurrentReplies(root));

        const next = await findNextButton(root);
        if (!next) break;
        try {
            next.click();
            await sleep(1000);
        } catch (e) {
            break;
        }
    }
    return allReplies;
}

// 主采集流程
async function extractComments() {
    try {
        updateStatus('开始采集流程...');
        await scrollToBottom();

        updateStatus('2/3 正在处理每条评论...');
        const root = document.querySelector('bili-comments');
        if (!root?.shadowRoot) throw new Error('找不到评论区域');

        const threads = root.shadowRoot.querySelectorAll('bili-comment-thread-renderer');
        const results = [];

        for (let i = 0; i < threads.length; i++) {
            const thread = threads[i];
            const shadow = thread.shadowRoot;
            if (!shadow) continue;

            updateStatus(`处理第 ${i + 1}/${threads.length} 条评论...`);

            // 主评论
            const main = getCommentText(shadow.querySelector('#comment'));
            // 所有回复 评论
            const replies = await processReplies(shadow, i);

            // ✅ 使用 m 和 r （main、replies）
            results.push({ m: main || '', r: replies });
            await sleep(20);
        }
        updateStatus(`3/3 完成采集，共 ${results.length} 条评论。`);
        chrome.runtime.sendMessage({ action: 'extractionComplete', data: results });
    } catch (err) {
        chrome.runtime.sendMessage({ action: 'extractionFailed', message: err.message });
    }
}

// 接收 popup 页面请求
chrome.runtime.onMessage.addListener((req, sender, res) => {
    if (req.action === 'startExtraction') {
        extractComments();
    }
});