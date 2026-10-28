package runners

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	topolvmv1 "github.com/topolvm/topolvm/api/v1"
	"github.com/topolvm/topolvm/pkg/lvmd/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// This prefix and version identify the closed provider budget transport schema.
const reservationAnnotationPrefix = "reservation.topolvm.io/"

type publishedAllocation struct {
	Name          string `json:"name"`
	PhysicalUUID  string `json:"physical_uuid"`
	ReservedBytes uint64 `json:"reserved_bytes"`
}
type publishedReservationBudget struct {
	Version                     int                   `json:"version"`
	ObservedAt                  string                `json:"observed_at"`
	RefreshAfterSeconds         uint64                `json:"refresh_after_seconds"`
	NodeUID                     string                `json:"node_uid"`
	PoolSizeBytes               uint64                `json:"pool_size_bytes"`
	OrdinaryBytes               uint64                `json:"ordinary_bytes"`
	EffectiveBackupReserveBytes uint64                `json:"effective_backup_reserve_bytes"`
	LargestSizeBytes            uint64                `json:"largest_size_bytes"`
	Allocations                 []publishedAllocation `json:"allocations"`
}

func (m *metricsExporter) annotateReservations(ctx context.Context, node *metav1.PartialObjectMetadata, items []*proto.WatchItem) error {
	guarded := false
	for _, item := range items {
		if item.ReservationBudget != nil {
			guarded = true
			break
		}
	}
	if !guarded {
		return nil
	}
	var volumes topolvmv1.LogicalVolumeList
	if err := m.client.List(ctx, &volumes); err != nil {
		return err
	}
	for _, item := range items {
		budget := item.ReservationBudget
		if budget == nil {
			continue
		}
		if _, err := time.Parse(time.RFC3339Nano, budget.ObservedAt); err != nil {
			return fmt.Errorf("provider budget observation time is unavailable: %w", err)
		}
		if node.UID == "" {
			return fmt.Errorf("provider budget node incarnation is unavailable")
		}
		frame := publishedReservationBudget{Version: 1, ObservedAt: budget.ObservedAt, RefreshAfterSeconds: budget.RefreshAfterSeconds, NodeUID: string(node.UID), PoolSizeBytes: budget.PoolSizeBytes, OrdinaryBytes: budget.OrdinaryBytes, EffectiveBackupReserveBytes: budget.EffectiveBackupReserveBytes, LargestSizeBytes: budget.LargestSizeBytes, Allocations: []publishedAllocation{}}
		for _, allocation := range budget.Allocations {
			if !allocation.Ordinary {
				continue
			}
			for _, lv := range volumes.Items {
				if lv.Spec.NodeName == m.nodeName && lv.Spec.DeviceClass == item.DeviceClass && string(lv.UID) == allocation.Name && lv.Status.VolumeID == allocation.Name && lv.Status.PhysicalUUID == allocation.PhysicalUuid {
					frame.Allocations = append(frame.Allocations, publishedAllocation{Name: lv.Name, PhysicalUUID: allocation.PhysicalUuid, ReservedBytes: allocation.ReservedBytes})
					break
				}
			}
		}
		data, err := json.Marshal(frame)
		if err != nil {
			return err
		}
		node.Annotations[reservationAnnotationPrefix+item.DeviceClass] = string(data)
	}
	return nil
}
