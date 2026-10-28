package lvmd

import (
	"context"
	"fmt"

	"github.com/topolvm/topolvm/internal/lvmd/command"
	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	lvmdTypes "github.com/topolvm/topolvm/pkg/lvmd/types"
)

type physicalInventory struct {
	total, quantum uint64
	volumes        map[string]*proto.LogicalVolume
}

// This boundary keeps tests at provider transactions while replacing only LVM.
type reservationBackend interface {
	inventory(context.Context) (*physicalInventory, error)
	create(context.Context, string, uint64, []string) error
	snapshot(context.Context, string, string, []string) error
	resize(context.Context, string, uint64) error
	activate(context.Context, string, string) error
	replaceTag(context.Context, string, string, string) error
	remove(context.Context, string) error
}

type lvmReservationBackend struct{ dc *lvmdTypes.DeviceClass }

func (b *lvmReservationBackend) inventory(ctx context.Context) (*physicalInventory, error) {
	vg, err := command.FindVolumeGroup(ctx, b.dc.VolumeGroup)
	if err != nil {
		return nil, err
	}
	pool, err := vg.FindPool(ctx, b.dc.ThinPoolConfig.Name)
	if err != nil {
		return nil, err
	}
	volumes, err := pool.ListVolumes(ctx)
	if err != nil {
		return nil, err
	}
	inventory := &physicalInventory{total: pool.Size(), quantum: vg.ExtentSize(), volumes: map[string]*proto.LogicalVolume{}}
	if inventory.total == 0 || inventory.quantum == 0 {
		return nil, fmt.Errorf("physical pool size or allocation quantum is unavailable")
	}
	for name, lv := range volumes {
		if lv.UUID() == "" {
			return nil, fmt.Errorf("physical volume UUID is unavailable")
		}
		inventory.volumes[name] = &proto.LogicalVolume{Name: name, PhysicalUuid: lv.UUID(), Pool: pool.Name(), SizeBytes: int64(lv.Size()), Tags: lv.Tags(), DevMajor: lv.MajorNumber(), DevMinor: lv.MinorNumber(), Path: lv.Path(), Attr: lv.Attr()}
	}
	return inventory, nil
}

func (b *lvmReservationBackend) volume(ctx context.Context, name string) (*command.LogicalVolume, error) {
	vg, err := command.FindVolumeGroup(ctx, b.dc.VolumeGroup)
	if err != nil {
		return nil, err
	}
	pool, err := vg.FindPool(ctx, b.dc.ThinPoolConfig.Name)
	if err != nil {
		return nil, err
	}
	return pool.FindVolume(ctx, name)
}
func (b *lvmReservationBackend) create(ctx context.Context, name string, size uint64, tags []string) error {
	pool, err := storagePoolForDeviceClass(ctx, b.dc)
	if err != nil {
		return err
	}
	var stripe uint
	if b.dc.Stripe != nil {
		stripe = *b.dc.Stripe
	}
	return pool.CreateVolume(ctx, name, size, tags, stripe, b.dc.StripeSize, nil)
}
func (b *lvmReservationBackend) snapshot(ctx context.Context, source, name string, tags []string) error {
	lv, err := b.volume(ctx, source)
	if err != nil {
		return err
	}
	return lv.ThinSnapshot(ctx, name, tags)
}
func (b *lvmReservationBackend) resize(ctx context.Context, name string, size uint64) error {
	lv, err := b.volume(ctx, name)
	if err != nil {
		return err
	}
	return lv.Resize(ctx, size)
}
func (b *lvmReservationBackend) activate(ctx context.Context, name, access string) error {
	lv, err := b.volume(ctx, name)
	if err != nil {
		return err
	}
	return lv.Activate(ctx, access)
}
func (b *lvmReservationBackend) replaceTag(ctx context.Context, name, old, next string) error {
	lv, err := b.volume(ctx, name)
	if err != nil {
		return err
	}
	return lv.ReplaceTag(ctx, old, next)
}
func (b *lvmReservationBackend) remove(ctx context.Context, name string) error {
	vg, err := command.FindVolumeGroup(ctx, b.dc.VolumeGroup)
	if err != nil {
		return err
	}
	return vg.RemoveVolume(ctx, name)
}

// Failed commands can commit physical intent; publication must observe those too.
type notifyingReservationBackend struct {
	reservationBackend
	notify func()
}

func (b notifyingReservationBackend) create(ctx context.Context, name string, size uint64, tags []string) error {
	defer b.notify()
	return b.reservationBackend.create(ctx, name, size, tags)
}
func (b notifyingReservationBackend) snapshot(ctx context.Context, source, name string, tags []string) error {
	defer b.notify()
	return b.reservationBackend.snapshot(ctx, source, name, tags)
}
func (b notifyingReservationBackend) resize(ctx context.Context, name string, size uint64) error {
	defer b.notify()
	return b.reservationBackend.resize(ctx, name, size)
}
func (b notifyingReservationBackend) replaceTag(ctx context.Context, name, old, next string) error {
	if old == next {
		return nil
	}
	defer b.notify()
	return b.reservationBackend.replaceTag(ctx, name, old, next)
}
func (b notifyingReservationBackend) remove(ctx context.Context, name string) error {
	defer b.notify()
	return b.reservationBackend.remove(ctx, name)
}
