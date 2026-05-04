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

            // 스트림 토큰을 이어 붙일 빈 어시스턴트 메시지를 미리 push.
            // push 후 배열 안의 참조(Alpine 반응성 Proxy)를 다시 받아와야
            // 이후 mutation 이 DOM 갱신을 트리거한다.
            this.messages.push({ role: 'assistant', content: '', sources: [] });
            var assistant = this.messages[this.messages.length - 1];

            try {
                var response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query: query })
                });
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                await this._consumeNdjsonStream(response, assistant);
                if (!assistant.content) {
                    assistant.content = '응답을 받지 못했습니다.';
                }
            } catch (err) {
                assistant.content = '오류가 발생했습니다: ' + err.message;
            } finally {
                this.loading = false;
                this.scrollToBottom();
            }
        },

        async _consumeNdjsonStream(response, assistant) {
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';
            while (true) {
                var chunk = await reader.read();
                if (chunk.done) break;
                buffer += decoder.decode(chunk.value, { stream: true });
                var newlineIdx;
                while ((newlineIdx = buffer.indexOf('\n')) !== -1) {
                    var line = buffer.slice(0, newlineIdx).trim();
                    buffer = buffer.slice(newlineIdx + 1);
                    if (!line) continue;
                    this._handleEvent(line, assistant);
                }
            }
            // 버퍼에 남은 마지막 라인 처리
            var tail = buffer.trim();
            if (tail) this._handleEvent(tail, assistant);
        },

        _handleEvent(line, assistant) {
            var event;
            try {
                event = JSON.parse(line);
            } catch (e) {
                return;
            }
            if (event.type === 'sources') {
                assistant.sources = event.sources || [];
            } else if (event.type === 'delta') {
                assistant.content += (event.content || '');
                this.scrollToBottom();
            } else if (event.type === 'error') {
                assistant.content = event.content || '오류가 발생했습니다.';
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
