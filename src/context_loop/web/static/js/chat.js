/* Context Loop Dashboard — Chat Interface */

function chatApp() {
    return {
        messages: [],
        input: '',
        loading: false,

        async sendMessage() {
            var query = this.input.trim();
            if (!query || this.loading) return;

            this.messages.push({ role: 'user', content: query, sources: [] });
            this.input = '';
            this.loading = true;
            this.scrollToBottom();

            try {
                var response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query: query })
                });
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                var data = await response.json();
                this.messages.push({
                    role: 'assistant',
                    content: data.answer || '응답을 받지 못했습니다.',
                    sources: data.sources || []
                });
            } catch (err) {
                this.messages.push({
                    role: 'assistant',
                    content: '오류가 발생했습니다: ' + err.message,
                    sources: []
                });
            } finally {
                this.loading = false;
                this.scrollToBottom();
            }
        },

        scrollToBottom() {
            this.$nextTick(function() {
                var container = document.getElementById('chat-messages');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            });
        },

        renderMarkdown(text) {
            if (!text) return '';
            // 간단한 마크다운 → HTML 변환
            return text
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
                .replace(/`(.+?)`/g, '<code>$1</code>')
                .replace(/\n/g, '<br>');
        }
    };
}
