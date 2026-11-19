import assert from 'node:assert/strict';
import { test } from 'node:test';
import { chiefDeliveryText } from '../../runtime/chief_delivery.mjs';

test('M08 native product notices render the observed release and restore facts', () => {
  const project = { project_title: '论文阅读助手' };
  assert.equal(chiefDeliveryText({
    ...project, kind: 'PRODUCT_RELEASED', summary: '默认入口已验证 v1.1.0。',
  }), '「论文阅读助手」已发布。\n默认入口已验证 v1.1.0。');
  assert.equal(chiefDeliveryText({
    ...project, kind: 'PRODUCT_ROLLED_BACK', summary: '默认入口已恢复 v1.0.0。',
  }), '「论文阅读助手」已回退。\n默认入口已恢复 v1.0.0。');
});
