//go:build !windows

package main

import "errors"

func protectSecret(string) (string, error) {
	return "", errors.New("DPAPI is available only in the Windows connector build")
}

func unprotectSecret(string) (string, error) {
	return "", errors.New("DPAPI is available only in the Windows connector build")
}
