cask "cloud-dupe" do
  version :latest
  sha256 :no_check

  on_arm do
    url "https://github.com/watanaberyunosuke/clouddupe/releases/latest/download/cloud-dupe-macos-arm64.dmg"
  end
  on_intel do
    url "https://github.com/watanaberyunosuke/clouddupe/releases/latest/download/cloud-dupe-macos-intel.dmg"
  end

  name "Cloud Dupe"
  desc "Find duplicate files across cloud drives without downloading them"
  homepage "https://github.com/watanaberyunosuke/clouddupe"

  depends_on macos: ">= :big_sur"

  app "Cloud Dupe.app"

  caveats <<~EOS
    Cloud Dupe is not signed with a paid Apple developer certificate.
    On first launch, Control-click the app in Applications and choose Open.
  EOS
end
