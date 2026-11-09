import { useState } from "react";
import { Button, Modal } from "../../components/console";
import { useSettingsDirty } from "./SettingsDrafts";

/** 同时覆盖取消、Escape、遮罩与路由离开；保存成功可直接关闭，不再次确认。 */
export function useDiscardChanges(dirty: boolean, close: () => void) {
  const [confirm, setConfirm] = useState(false);
  useSettingsDirty(dirty);
  return {
    requestClose: () => {
      if (dirty) setConfirm(true);
      else close();
    },
    confirmation: (
      <Modal
        visible={confirm}
        title="放弃未保存的修改？"
        onCancel={() => setConfirm(false)}
        width={440}
        footer={null}
      >
        <p className="form-help">关闭后本次输入不会保存。</p>
        <div className="modal-actions">
          <Button onClick={() => setConfirm(false)}>继续编辑</Button>
          <Button
            type="danger"
            onClick={() => {
              setConfirm(false);
              close();
            }}
          >
            放弃修改
          </Button>
        </div>
      </Modal>
    ),
  };
}
