/** jsdom不实现原生dialog；这里只补开关属性，焦点/模态/键盘行为用真实浏览器验收。 */
Object.defineProperties(HTMLDialogElement.prototype, {
  showModal: {
    configurable: true,
    value() {
      this.setAttribute('open', '');
    },
  },
  close: {
    configurable: true,
    value() {
      this.removeAttribute('open');
    },
  },
});
