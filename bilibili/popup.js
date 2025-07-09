document.addEventListener('DOMContentLoaded', () => {
    const startJsonButton = document.getElementById('startJsonButton');
    const startMdButton = document.getElementById('startMdButton');
    const statusDiv = document.getElementById('status');
    let currentFormat = 'json'; // 存储用户选择的格式

    // 禁用/启用按钮的辅助函数
    const setButtonsDisabled = (disabled) => {
        startJsonButton.disabled = disabled;
        startMdButton.disabled = disabled;
    };

    // 开始采集的通用逻辑
    const startExtraction = async (format) => {
        currentFormat = format;
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

        if (tab && tab.url.includes('bilibili.com/video/')) {
            setButtonsDisabled(true);
            statusDiv.textContent = `正在初始化 (${format.toUpperCase()})...`;

            chrome.scripting.executeScript({
                target: { tabId: tab.id },
                files: ['content.js']
            }).then(() => {
                statusDiv.textContent = '脚本注入成功，开始执行...';
                // 发送开始指令
                chrome.tabs.sendMessage(tab.id, { action: "startExtraction" });
            }).catch(err => {
                statusDiv.textContent = '错误：无法注入脚本。请刷新页面重试。';
                console.error('Script injection failed:', err);
                setButtonsDisabled(false);
            });
        } else {
            statusDiv.textContent = '请在B站视频页面使用此插件。';
        }
    };

    startJsonButton.addEventListener('click', () => startExtraction('json'));
    startMdButton.addEventListener('click', () => startExtraction('md'));

    // 将数据转换为Markdown格式
    const convertToMarkdown = (data) => {
        let mdContent = `# B站评论采集\n\n`;
        data.forEach((item, index) => {
            mdContent += `## 楼层 ${index + 1}\n\n`;
            // 使用简化的键名 m (main_comment)
            mdContent += `[M]: ${item.m}\n\n`; 
            
            if (item.r && item.r.length > 0) {
                // 使用简化的键名 r (replies)
                item.r.forEach(reply => {
                    // 使用引用格式表示回复
                    mdContent += `> [R]: ${reply.replace(/\n/g, '\n> ')}\n`;
                });
                mdContent += '\n';
            }
        });
        return mdContent;
    };

    // 处理下载
    const downloadData = (data) => {
        let fileContent, mimeType, extension;

        if (currentFormat === 'json') {
            fileContent = JSON.stringify(data, null, 2);
            mimeType = 'application/json;charset=utf-8';
            extension = 'json';
        } else if (currentFormat === 'md') {
            fileContent = convertToMarkdown(data);
            mimeType = 'text/markdown;charset=utf-8';
            extension = 'md';
        }

        const blob = new Blob([fileContent], { type: mimeType });
        const url = URL.createObjectURL(blob);
        const filename = `bilibili_comments_${new Date().getTime()}.${extension}`;

        chrome.downloads.download({
            url: url,
            filename: filename
        }, () => {
            URL.revokeObjectURL(url);
            statusDiv.textContent = '下载完成！';
            setButtonsDisabled(false);
        });
    };

    // 监听来自content.js的消息
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === "updateStatus") {
            statusDiv.textContent = request.message;
        } else if (request.action === "extractionComplete") {
            statusDiv.textContent = '采集完成！准备下载...';
            downloadData(request.data);
        } else if (request.action === "extractionFailed") {
            statusDiv.textContent = `采集失败：${request.message}`;
            setButtonsDisabled(false);
        }
    });
});