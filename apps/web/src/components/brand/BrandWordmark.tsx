import "./brand.css";

/** 对齐 Figma 的文字标识；登录和控制台共用，避免各自拼出不同 Logo。 */
export function BrandWordmark() {
  return (
    <span className="weora-wordmark" aria-hidden="true">
      <span className="weora-wordmark-mark">w·</span>
      <span className="weora-wordmark-name">weora</span>
    </span>
  );
}
