# Disposable test rig for claude-realms: Arch (same packages as the user's
# Omarchy host), systemd as PID 1 with a lingering user manager so realm scopes
# work exactly as on a real desktop. Run unprivileged (see run.sh).
FROM archlinux:latest
RUN pacman -Syu --noconfirm --needed \
      systemd dbus labwc wayvnc grim wlr-randr bubblewrap xorg-xwayland \
      at-spi2-core glib2 python uv nodejs npm git tmux sudo which procps-ng \
      gtk3 gtk3-demos xterm foot ttf-dejavu libxi libx11 libxkbcommon \
      qemu-base edk2-ovmf mtools openssh jq kitty mesa xorg-xdpyinfo \
    && pacman -Scc --noconfirm
# Unprivileged test user with the same uid as the host user so bind mounts
# keep sane ownership; linger keeps user@1000 running without a login.
RUN useradd -m -u 1000 -s /bin/bash tester \
    && mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/tester \
    && systemctl mask systemd-firstboot.service systemd-udevd.service \
         systemd-networkd-wait-online.service getty@tty1.service console-getty.service \
    && systemctl set-default multi-user.target
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/lib/systemd/systemd"]
