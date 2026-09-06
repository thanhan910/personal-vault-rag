//go:build windows

package main

import (
	"encoding/base64"
	"fmt"
	"syscall"
	"unsafe"
)

type dataBlob struct {
	cbData uint32
	pbData *byte
}

var crypt32 = syscall.NewLazyDLL("crypt32.dll")
var kernel32 = syscall.NewLazyDLL("kernel32.dll")
var cryptProtectData = crypt32.NewProc("CryptProtectData")
var cryptUnprotectData = crypt32.NewProc("CryptUnprotectData")
var localFree = kernel32.NewProc("LocalFree")

func blob(data []byte) dataBlob {
	if len(data) == 0 {
		return dataBlob{}
	}
	return dataBlob{uint32(len(data)), &data[0]}
}
func blobBytes(value dataBlob) []byte {
	if value.cbData == 0 {
		return nil
	}
	return append([]byte(nil), unsafe.Slice(value.pbData, value.cbData)...)
}

func protectSecret(value string) (string, error) {
	in := blob([]byte(value))
	var out dataBlob
	r, _, err := cryptProtectData.Call(uintptr(unsafe.Pointer(&in)), 0, 0, 0, 0, 1, uintptr(unsafe.Pointer(&out)))
	if r == 0 {
		return "", fmt.Errorf("CryptProtectData: %w", err)
	}
	defer localFree.Call(uintptr(unsafe.Pointer(out.pbData)))
	return base64.StdEncoding.EncodeToString(blobBytes(out)), nil
}

func unprotectSecret(value string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		return "", err
	}
	in := blob(raw)
	var out dataBlob
	r, _, callErr := cryptUnprotectData.Call(uintptr(unsafe.Pointer(&in)), 0, 0, 0, 0, 1, uintptr(unsafe.Pointer(&out)))
	if r == 0 {
		return "", fmt.Errorf("CryptUnprotectData: %w", callErr)
	}
	defer localFree.Call(uintptr(unsafe.Pointer(out.pbData)))
	return string(blobBytes(out)), nil
}
